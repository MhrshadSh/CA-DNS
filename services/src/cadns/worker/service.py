"""Long-running measurement worker (Phase 4).

Claims queued domains and measures up to `concurrency` of them at a time.
Woken by NOTIFY on the queue channel, with polling as a fallback (retries whose
backoff has passed, missed notifications, database restarts).

Shutdown: stop claiming, give running measurements `grace_seconds` to finish,
then cancel the rest and release their jobs so another worker can take them.
"""

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable

import psycopg
import psycopg_pool

from cadns import queue
from cadns.queue import Job, QueuePolicy
from cadns.worker.pipeline import Measurement

log = logging.getLogger(__name__)

MeasureFn = Callable[[psycopg.AsyncConnection, str], Awaitable[Measurement]]


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(
        self,
        pool: psycopg_pool.AsyncConnectionPool,
        measure: MeasureFn,
        policy: QueuePolicy,
        *,
        concurrency: int = 4,
        poll_seconds: float = 10.0,
        grace_seconds: float = 20.0,
        listen: Callable[[], Awaitable[psycopg.AsyncConnection]] | None = None,
    ) -> None:
        self.pool = pool
        self.measure = measure
        self.policy = policy
        self.concurrency = concurrency
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds
        self._listen_connect = listen
        self.id = worker_id()
        self._wake = asyncio.Event()
        self._running: dict[asyncio.Task, Job] = {}
        self.processed = 0

    def wake(self) -> None:
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker %s started (concurrency %d)", self.id, self.concurrency)
        listener = asyncio.create_task(self._listen()) if self._listen_connect else None
        stopper = asyncio.create_task(self._wake_on(stop))
        try:
            while not stop.is_set():
                self._wake.clear()
                claimed = await self._claim()
                if claimed and len(self._running) < self.concurrency:
                    continue  # the queue may hold more ready jobs
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
        finally:
            stopper.cancel()
            if listener is not None:
                listener.cancel()
            await self._drain()
            log.info("worker %s stopped after %d job(s)", self.id, self.processed)

    async def _wake_on(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self._wake.set()

    async def _claim(self) -> int:
        free = self.concurrency - len(self._running)
        if free <= 0:
            return 0
        try:
            async with self.pool.connection() as conn:
                jobs = await queue.claim(conn, self.id, free, self.policy)
        except (psycopg.Error, psycopg_pool.PoolTimeout) as exc:
            log.warning("claiming jobs failed: %s", exc)
            return 0
        for job in jobs:
            task = asyncio.create_task(self._process(job), name=f"measure {job.domain}")
            self._running[task] = job
            task.add_done_callback(self._done)
        return len(jobs)

    def _done(self, task: asyncio.Task) -> None:
        self._running.pop(task, None)
        self._wake.set()

    async def _process(self, job: Job) -> None:
        try:
            async with self.pool.connection() as conn:
                result = await self.measure(conn, job.domain)
                if result.outcome.status == "failed":
                    state = await queue.fail(
                        conn, job, self.id, "no definitive answer for A or AAAA", self.policy
                    )
                    log.warning(
                        "measuring %s failed (attempt %d, now %s)", job.domain, job.attempts, state
                    )
                else:
                    await queue.complete(conn, job, self.id)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(job))
            raise
        except Exception as exc:
            log.exception("measuring %s raised", job.domain)
            try:
                async with self.pool.connection() as conn:
                    await queue.fail(
                        conn, job, self.id, f"{type(exc).__name__}: {exc}", self.policy
                    )
            except (psycopg.Error, psycopg_pool.PoolTimeout):
                log.error("could not record the failure of %s; its lock will expire", job.domain)
        finally:
            self.processed += 1

    async def _release(self, job: Job) -> None:
        try:
            async with self.pool.connection(timeout=5) as conn:
                await queue.release(conn, job, self.id)
        except (psycopg.Error, psycopg_pool.PoolTimeout):
            log.error("could not release %s; its lock will expire", job.domain)

    async def _drain(self) -> None:
        if not self._running:
            return
        tasks = list(self._running)
        log.info(
            "waiting up to %.0fs for %d running measurement(s)", self.grace_seconds, len(tasks)
        )
        _, pending = await asyncio.wait(tasks, timeout=self.grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _listen(self) -> None:
        """Wake up on NOTIFY; reconnect with a delay if the connection drops."""
        while True:
            try:
                conn = await self._listen_connect()
                async with conn:
                    await conn.execute(f"LISTEN {queue.CHANNEL}")
                    self._wake.set()  # jobs may have been queued while disconnected
                    async for _ in conn.notifies():
                        self._wake.set()
            except psycopg.Error as exc:
                log.warning("queue listener disconnected: %s", exc)
            await asyncio.sleep(2)
