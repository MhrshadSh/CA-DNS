"""Queue semantics against PostgreSQL (committed data in the test database)."""

import asyncio

import psycopg
import pytest
from cadns import queue
from cadns.queue import QueuePolicy

pytestmark = pytest.mark.db

POLICY = QueuePolicy(
    lock_timeout_seconds=300,
    max_attempts=3,
    retry_base_seconds=30,
    retry_max_seconds=100,
    dead_cooldown_seconds=3600,
)


async def rows(conn, sql, *params):
    return await (await conn.execute(sql, params)).fetchall()


async def test_enqueue_deduplicates_and_notifies(committed):
    listener = await psycopg.AsyncConnection.connect(autocommit=True)
    async with listener:
        await listener.execute(f"LISTEN {queue.CHANNEL}")

        first = await queue.enqueue(committed, ["b.p4.test", "a.p4.test", "a.p4.test"], "miss")
        second = await queue.enqueue(committed, ["a.p4.test", "c.p4.test"], "expired")

        notifications = []
        async for note in listener.notifies(timeout=2, stop_after=2):
            notifications.append(note.channel)

    assert first == ["a.p4.test", "b.p4.test"]
    assert second == ["c.p4.test"]
    assert notifications == [queue.CHANNEL, queue.CHANNEL]
    assert await rows(
        committed, "SELECT domain, reason FROM cadns.measurement_queue ORDER BY 1"
    ) == [
        ("a.p4.test", "miss"),
        ("b.p4.test", "miss"),
        ("c.p4.test", "expired"),
    ]


async def test_nothing_to_enqueue_sends_no_notification(committed):
    assert await queue.enqueue(committed, [], "miss") == []


async def test_claim_is_fifo_and_locks(committed):
    for name in ("first.p4.test", "second.p4.test", "third.p4.test"):
        await queue.enqueue(committed, [name], "miss")

    jobs = await queue.claim(committed, "w1", 2, POLICY)

    assert [j.domain for j in jobs] == ["first.p4.test", "second.p4.test"]
    assert all(j.attempts == 1 for j in jobs)
    assert await queue.claim(committed, "w2", 5, POLICY) == [queue.Job("third.p4.test", "miss", 1)]
    assert await queue.claim(committed, "w3", 5, POLICY) == []


async def test_concurrent_workers_never_get_the_same_job(committed):
    await queue.enqueue(committed, [f"d{i:02}.p4.test" for i in range(40)], "miss")
    conns = [await psycopg.AsyncConnection.connect(autocommit=True) for _ in range(4)]
    try:
        results = await asyncio.gather(
            *(queue.claim(c, f"w{i}", 15, POLICY) for i, c in enumerate(conns))
        )
    finally:
        for c in conns:
            await c.close()

    claimed = [job.domain for jobs in results for job in jobs]
    assert len(claimed) == len(set(claimed)) == 40


async def test_complete_only_by_the_owner(committed):
    await queue.enqueue(committed, ["x.p4.test"], "miss")
    (job,) = await queue.claim(committed, "owner", 1, POLICY)

    await queue.complete(committed, job, "someone-else")
    assert await rows(committed, "SELECT count(*) FROM cadns.measurement_queue") == [(1,)]

    await queue.complete(committed, job, "owner")
    assert await rows(committed, "SELECT count(*) FROM cadns.measurement_queue") == [(0,)]


async def test_fail_backs_off_exponentially_then_dead_letters(committed):
    await queue.enqueue(committed, ["flaky.p4.test"], "miss")
    delays = []
    for _ in range(POLICY.max_attempts):
        await committed.execute(
            "UPDATE cadns.measurement_queue SET next_attempt_at = now() "
            "WHERE domain = 'flaky.p4.test'"
        )
        (job,) = await queue.claim(committed, "w", 1, POLICY)
        state = await queue.fail(committed, job, "w", "boom", POLICY)
        ((delay, stored_state, error, locked),) = await rows(
            committed,
            """SELECT round(extract(epoch FROM next_attempt_at - now())), state, last_error,
                      locked_by IS NOT NULL
               FROM cadns.measurement_queue WHERE domain = 'flaky.p4.test'""",
        )
        assert (state, stored_state, error, locked) == (stored_state, stored_state, "boom", False)
        delays.append((int(delay), state))

    assert delays == [(30, "pending"), (60, "pending"), (3600, "dead")]


async def test_backoff_is_capped():
    assert POLICY.retry_delay(10) == POLICY.retry_max_seconds


async def test_dead_job_is_revived_by_a_miss_only_after_cooldown(committed):
    await queue.enqueue(committed, ["dead.p4.test"], "miss")
    await committed.execute(
        """UPDATE cadns.measurement_queue
           SET state = 'dead', attempts = 3, next_attempt_at = now() + interval '1 hour'
           WHERE domain = 'dead.p4.test'"""
    )

    assert await queue.enqueue(committed, ["dead.p4.test"], "miss") == []

    await committed.execute(
        "UPDATE cadns.measurement_queue SET next_attempt_at = now() - interval '1 s'"
    )
    assert await queue.enqueue(committed, ["dead.p4.test"], "miss") == ["dead.p4.test"]
    assert await rows(
        committed, "SELECT state, attempts, last_error FROM cadns.measurement_queue"
    ) == [("pending", 0, None)]


async def test_stale_lock_can_be_claimed_again(committed):
    await queue.enqueue(committed, ["stuck.p4.test"], "miss")
    await queue.claim(committed, "crashed", 1, POLICY)

    assert await queue.claim(committed, "w2", 1, POLICY) == []

    await committed.execute(
        "UPDATE cadns.measurement_queue SET locked_at = now() - interval '301 s'"
    )
    (job,) = await queue.claim(committed, "w2", 1, POLICY)
    assert job.attempts == 2


async def test_release_returns_the_job_without_counting_an_attempt(committed):
    await queue.enqueue(committed, ["released.p4.test"], "miss")
    (job,) = await queue.claim(committed, "w", 1, POLICY)

    await queue.release(committed, job, "w")

    assert await rows(committed, "SELECT attempts, locked_by FROM cadns.measurement_queue") == [
        (0, None)
    ]
