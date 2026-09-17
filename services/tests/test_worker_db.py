"""Worker service against the real queue, with a fake measurement function."""

import asyncio

import psycopg
import psycopg_pool
import pytest
from cadns import queue
from cadns.queue import QueuePolicy
from cadns.worker.pipeline import Measurement, Outcome
from cadns.worker.service import Worker

pytestmark = pytest.mark.db

POLICY = QueuePolicy(retry_base_seconds=30)


class FakeMeasure:
    def __init__(self):
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def __call__(self, conn, domain):
        self.calls.append(domain)
        self.started.set()
        if domain.startswith("boom."):
            raise RuntimeError("upstream exploded")
        if domain.startswith("slow."):
            await asyncio.sleep(60)
        status = "failed" if domain.startswith("fail.") else "resolved"
        return Measurement(domain, Outcome(status, ()))


@pytest.fixture
async def pool(committed):
    pool = psycopg_pool.AsyncConnectionPool(
        "", min_size=1, max_size=5, kwargs={"autocommit": True}, open=False
    )
    await pool.open()
    yield pool
    await pool.close()


def make_worker(pool, measure, **kwargs):
    return Worker(
        pool,
        measure,
        POLICY,
        listen=lambda: psycopg.AsyncConnection.connect(autocommit=True),
        **kwargs,
    )


async def wait_until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.05)


async def queue_rows(conn):
    return await (
        await conn.execute(
            "SELECT domain, state, attempts, locked_by IS NOT NULL, last_error "
            "FROM cadns.measurement_queue ORDER BY 1"
        )
    ).fetchall()


async def test_worker_measures_everything_in_the_queue(committed, pool):
    names = [f"ok{i}.p4.test" for i in range(7)]
    await queue.enqueue(committed, names, "miss")
    measure = FakeMeasure()
    worker = make_worker(pool, measure, concurrency=3, poll_seconds=0.2)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))

    await wait_until(lambda: _empty(committed))
    stop.set()
    await running

    assert sorted(measure.calls) == names
    assert worker.processed == 7


async def _empty(conn):
    return (await queue_rows(conn)) == []


async def test_failed_and_raising_measurements_are_retried_later(committed, pool):
    await queue.enqueue(committed, ["fail.p4.test", "boom.p4.test"], "miss")
    worker = make_worker(pool, FakeMeasure(), poll_seconds=0.2)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))

    async def both_failed():
        return all(not locked and error for _, _, _, locked, error in await queue_rows(committed))

    await wait_until(both_failed)
    stop.set()
    await running

    assert await queue_rows(committed) == [
        ("boom.p4.test", "pending", 1, False, "RuntimeError: upstream exploded"),
        ("fail.p4.test", "pending", 1, False, "no definitive answer for A or AAAA"),
    ]


async def test_notify_wakes_an_idle_worker(committed, pool):
    measure = FakeMeasure()
    worker = make_worker(pool, measure, poll_seconds=60)  # polling alone would take a minute
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.5)  # idle, listening

    await queue.enqueue(committed, ["woken.p4.test"], "miss")
    await asyncio.wait_for(measure.started.wait(), timeout=3)

    stop.set()
    await running
    assert measure.calls == ["woken.p4.test"]


async def test_shutdown_releases_unfinished_jobs(committed, pool):
    await queue.enqueue(committed, ["slow.p4.test"], "miss")
    measure = FakeMeasure()
    worker = make_worker(pool, measure, poll_seconds=0.2, grace_seconds=0.2)
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(measure.started.wait(), timeout=3)

    stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert await queue_rows(committed) == [("slow.p4.test", "pending", 0, False, None)]
