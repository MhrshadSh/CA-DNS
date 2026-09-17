"""Monitor scheduling on a fake clock: hours of schedule in milliseconds."""

import asyncio
import itertools
from datetime import UTC, datetime, timedelta

from cadns.monitor.service import Clock, Monitor, Schedule

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock(Clock):
    def __init__(self):
        self.t = 0.0
        super().__init__(wall=lambda: T0 + timedelta(seconds=self.t), monotonic=lambda: self.t)

    async def sleep(self, seconds, stop):
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


def recorder(clock, log, name, *, duration=0.0, fail=False):
    async def job(now):
        log.append((name, round(clock.t, 3), now))
        clock.t += duration
        if fail:
            raise RuntimeError("boom")

    return job


async def run_for(monitor, clock, seconds):
    stop = asyncio.Event()

    async def stopper(now):
        if clock.t >= seconds:
            stop.set()

    monitor.schedules.append(Schedule("stopper", 1.0, stopper))
    await asyncio.wait_for(monitor.run(stop), timeout=10)


async def test_jobs_run_at_their_intervals_for_two_simulated_hours():
    clock, log = FakeClock(), []
    monitor = Monitor(
        [
            Schedule("scan", 5, recorder(clock, log, "scan")),
            Schedule("carbon", 60, recorder(clock, log, "carbon")),
            Schedule("gc", 3600, recorder(clock, log, "gc")),
        ],
        clock,
    )

    await run_for(monitor, clock, 7200)

    times = {name: [t for n, t, _ in log if n == name] for name in ("scan", "carbon", "gc")}
    assert times["scan"][:4] == [0, 5, 10, 15]
    assert len(times["scan"]) == 1441
    assert len(times["carbon"]) == 121
    assert times["gc"] == [0, 3600, 7200]
    # jobs see the wall clock matching the simulated time
    assert all(now == T0 + timedelta(seconds=t) for _, t, now in log)


async def test_failing_job_does_not_stop_the_others():
    clock, log = FakeClock(), []
    monitor = Monitor(
        [
            Schedule("broken", 10, recorder(clock, log, "broken", fail=True)),
            Schedule("scan", 5, recorder(clock, log, "scan")),
        ],
        clock,
    )

    await run_for(monitor, clock, 60)

    assert monitor.runs["broken"] == 7
    assert monitor.runs["scan"] == 13


async def test_slow_job_does_not_cause_a_burst_of_catch_up_runs():
    clock, log = FakeClock(), []
    monitor = Monitor([Schedule("slow", 5, recorder(clock, log, "slow", duration=12))], clock)

    await run_for(monitor, clock, 60)

    starts = [t for _, t, _ in log]
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert min(gaps) >= 12  # never back-to-back to catch up missed intervals


async def test_stop_ends_the_real_clock_sleep_immediately():
    monitor = Monitor([Schedule("idle", 3600, lambda now: asyncio.sleep(0))])
    stop = asyncio.Event()
    task = asyncio.create_task(monitor.run(stop))
    await asyncio.sleep(0.1)

    stop.set()
    await asyncio.wait_for(task, timeout=1)
