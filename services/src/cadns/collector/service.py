"""Collector service: dnstap socket -> batched database writes.

- hits:   domains.last_queried_at / hit_count
- misses: upsert the domain as 'pending' (records query activity even before
          the first measurement), then enqueue it unless its retry_after
          (negative or failed measurement) is still in the future, or it was
          measured after the miss (a client retrying while the worker ran).

Events are aggregated per name and flushed every flush_seconds. dnstap is
lossy by design, and so is the collector: if a flush fails the batch is
dropped (the next miss enqueues the name again).
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from cadns import metrics, queue
from cadns.collector.dnstap import HIT, ProtobufError, classify, decode_client_response
from cadns.collector.framestreams import FrameStreamsError, serve_connection
from cadns.health import Heartbeat

log = logging.getLogger(__name__)


@dataclass
class Batch:
    hits: dict[str, tuple[int, float]] = field(default_factory=dict)  # name -> (count, last time)
    misses: dict[str, float] = field(default_factory=dict)  # name -> last time

    def __len__(self) -> int:
        return len(self.hits) + len(self.misses)

    def add(self, kind: str, name: str, when: float) -> None:
        if kind == HIT:
            count, last = self.hits.get(name, (0, 0.0))
            self.hits[name] = (count + 1, max(last, when))
        else:
            self.misses[name] = max(self.misses.get(name, 0.0), when)


def _ts(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


async def write_batch(conn: psycopg.AsyncConnection, batch: Batch) -> list[str]:
    """Apply one batch; returns the domains that were (re)queued."""
    if batch.hits:
        names = sorted(batch.hits)
        await conn.execute(
            """UPDATE cadns.domains d
               SET last_queried_at = greatest(d.last_queried_at, s.queried_at),
                   hit_count = d.hit_count + s.hits
               FROM unnest(%s::text[], %s::timestamptz[], %s::bigint[])
                    AS s (name, queried_at, hits)
               WHERE d.name = s.name""",
            (
                names,
                [_ts(batch.hits[n][1]) for n in names],
                [batch.hits[n][0] for n in names],
            ),
        )
    if not batch.misses:
        return []

    names = sorted(batch.misses)
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """WITH seen AS (
                   SELECT * FROM unnest(%s::text[], %s::timestamptz[]) AS s (name, queried_at)
               ),
               upserted AS (
                   INSERT INTO cadns.domains (name, status, last_queried_at)
                   SELECT name, 'pending', queried_at FROM seen
                   ON CONFLICT (name) DO UPDATE
                   SET last_queried_at = greatest(domains.last_queried_at, EXCLUDED.last_queried_at)
                   RETURNING name, retry_after, measured_at
               )
               SELECT u.name FROM upserted u JOIN seen s USING (name)
               WHERE (u.retry_after IS NULL OR u.retry_after <= now())
                 AND (u.measured_at IS NULL OR u.measured_at < s.queried_at)
               ORDER BY u.name""",
            (names, [_ts(batch.misses[n]) for n in names]),
        )
        due = [name for (name,) in await cur.fetchall()]
        return await queue.enqueue(conn, due, "miss")


class Collector:
    def __init__(
        self,
        socket_path: Path,
        ignore_suffixes: frozenset[str],
        connect: Callable[[], "asyncio.Future[psycopg.AsyncConnection]"],
        flush_seconds: float = 1.0,
        max_batch: int = 5000,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.ignore_suffixes = ignore_suffixes
        self._connect = connect
        self.flush_seconds = flush_seconds
        self.max_batch = max_batch
        self.heartbeat = heartbeat
        self._batch = Batch()
        self._full = asyncio.Event()
        self._conn: psycopg.AsyncConnection | None = None

    def on_frame(self, frame: bytes) -> None:
        try:
            response = decode_client_response(frame)
        except ProtobufError as exc:
            log.debug("undecodable dnstap frame: %s", exc)
            return
        if response is None:
            return
        event = classify(response, self.ignore_suffixes)
        if event is None:
            return
        metrics.client_responses.labels(kind=event.kind).inc()
        self._batch.add(event.kind, event.name, event.time)
        if len(self._batch) >= self.max_batch:
            self._full.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("dnstap writer connected")
        try:
            frames = await serve_connection(reader, writer, self.on_frame)
            log.info("dnstap writer disconnected after %d frames", frames)
        except FrameStreamsError as exc:
            log.warning("dnstap session aborted: %s", exc)
        finally:
            writer.close()

    async def flush(self) -> None:
        batch, self._batch = self._batch, Batch()
        self._full.clear()
        if not batch:
            return
        try:
            if self._conn is None or self._conn.closed:
                self._conn = await self._connect()
            queued = await write_batch(self._conn, batch)
        except psycopg.Error as exc:
            log.error("dropping batch of %d names: %s", len(batch), exc)
            if self._conn is not None:
                await self._conn.close()
            self._conn = None
            return
        if queued:
            metrics.queue_operations.labels(operation="enqueued").inc(len(queued))
            log.info("queued %d domain(s): %s", len(queued), ", ".join(queued[:5]))

    async def run(self, stop: asyncio.Event) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        # BIND runs as another user in another container and must be able to connect.
        os.chmod(self.socket_path, 0o666)
        log.info("listening for dnstap on %s", self.socket_path)
        try:
            while not stop.is_set():
                waiters = [asyncio.ensure_future(e.wait()) for e in (stop, self._full)]
                await asyncio.wait(
                    waiters, timeout=self.flush_seconds, return_when="FIRST_COMPLETED"
                )
                for waiter in waiters:
                    waiter.cancel()
                await self.flush()
                if self.heartbeat is not None:
                    self.heartbeat.beat()
        finally:
            server.close()
            await self.flush()
            if self._conn is not None:
                await self._conn.close()
            self.socket_path.unlink(missing_ok=True)
