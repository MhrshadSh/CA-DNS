"""Measurement queue on cadns.measurement_queue.

- One row per domain: enqueueing a queued domain is a no-op (deduplication).
- claim() hands out the oldest ready rows with FOR UPDATE SKIP LOCKED, so any
  number of workers can consume concurrently. Locks older than the lock
  timeout (a crashed worker) can be claimed again.
- complete() removes the row; fail() schedules a retry with exponential
  backoff, or dead-letters the row after max_attempts. A dead row is
  re-enqueued by a new miss once its cooldown has passed.
- Enqueueing sends NOTIFY on CHANNEL (delivered on commit) to wake workers.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row

CHANNEL = "cadns_queue"


@dataclass(frozen=True)
class Job:
    domain: str
    reason: str
    attempts: int  # including the current one


@dataclass(frozen=True)
class QueuePolicy:
    lock_timeout_seconds: int = 300
    max_attempts: int = 5
    retry_base_seconds: int = 30
    retry_max_seconds: int = 3600
    dead_cooldown_seconds: int = 86400

    def retry_delay(self, attempts: int) -> int:
        return min(self.retry_max_seconds, self.retry_base_seconds * 2 ** max(0, attempts - 1))


async def enqueue(conn: psycopg.AsyncConnection, domains: Iterable[str], reason: str) -> list[str]:
    """Queue domains (already normalised); returns those newly queued or revived."""
    domains = sorted(set(domains))
    if not domains:
        return []
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO cadns.measurement_queue (domain, reason)
               SELECT unnest(%s::text[]), %s
               ON CONFLICT (domain) DO UPDATE
               SET state = 'pending', reason = EXCLUDED.reason, attempts = 0,
                   enqueued_at = now(), next_attempt_at = now(), last_error = NULL,
                   locked_by = NULL, locked_at = NULL
               WHERE measurement_queue.state = 'dead'
                 AND measurement_queue.next_attempt_at <= now()
               RETURNING domain""",
            (domains, reason),
        )
        queued = [row[0] for row in await cur.fetchall()]
        if queued:
            await cur.execute("SELECT pg_notify(%s, '')", (CHANNEL,))
    return queued


async def claim(
    conn: psycopg.AsyncConnection, worker_id: str, limit: int, policy: QueuePolicy
) -> list[Job]:
    if limit <= 0:
        return []
    async with conn.transaction(), conn.cursor(row_factory=class_row(Job)) as cur:
        await cur.execute(
            """UPDATE cadns.measurement_queue q
               SET locked_by = %(worker)s, locked_at = now(), attempts = q.attempts + 1
               FROM (
                   SELECT domain FROM cadns.measurement_queue
                   WHERE state = 'pending'
                     AND next_attempt_at <= now()
                     AND (locked_at IS NULL
                          OR locked_at < now() - make_interval(secs => %(lock_timeout)s))
                   ORDER BY enqueued_at
                   LIMIT %(limit)s
                   FOR UPDATE SKIP LOCKED
               ) ready
               WHERE q.domain = ready.domain
               RETURNING q.domain, q.reason, q.attempts""",
            {"worker": worker_id, "limit": limit, "lock_timeout": policy.lock_timeout_seconds},
        )
        jobs = await cur.fetchall()
    return sorted(jobs, key=lambda job: job.domain)


async def complete(conn: psycopg.AsyncConnection, job: Job, worker_id: str) -> None:
    await conn.execute(
        "DELETE FROM cadns.measurement_queue WHERE domain = %s AND locked_by = %s",
        (job.domain, worker_id),
    )


async def fail(
    conn: psycopg.AsyncConnection, job: Job, worker_id: str, error: str, policy: QueuePolicy
) -> str:
    """Schedule a retry or dead-letter the job; returns the new state."""
    dead = job.attempts >= policy.max_attempts
    delay = policy.dead_cooldown_seconds if dead else policy.retry_delay(job.attempts)
    await conn.execute(
        """UPDATE cadns.measurement_queue
           SET state = %s, next_attempt_at = now() + make_interval(secs => %s),
               last_error = %s, locked_by = NULL, locked_at = NULL
           WHERE domain = %s AND locked_by = %s""",
        ("dead" if dead else "pending", delay, error[:1000], job.domain, worker_id),
    )
    return "dead" if dead else "pending"


async def release(conn: psycopg.AsyncConnection, job: Job, worker_id: str) -> None:
    """Give a claimed job back unprocessed (shutdown): no attempt is counted."""
    await conn.execute(
        """UPDATE cadns.measurement_queue
           SET locked_by = NULL, locked_at = NULL, attempts = greatest(0, attempts - 1)
           WHERE domain = %s AND locked_by = %s""",
        (job.domain, worker_id),
    )
