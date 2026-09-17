"""Monitor service: runs the periodic jobs (docs/architecture.md ADR-10).

    expiry scan     every scan_seconds (default 5 s)
    carbon refresh  every carbon_check_seconds (default 60 s); fetches only
                    regions missing the current 5-minute point
    GC              every gc_seconds (default 1 h)

Jobs run one after another in a single loop. Time comes from an injectable
clock and sleep function, so tests can simulate hours in milliseconds. A job
that raises is logged and retried at its next due time.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

Job = Callable[[datetime], Awaitable[None]]


@dataclass
class Schedule:
    name: str
    interval_seconds: float
    job: Job
    next_run: float = 0.0  # monotonic time; 0 = run at start


@dataclass
class Clock:
    """Wall clock for `now`, monotonic clock for scheduling, and a stop-aware sleep."""

    wall: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic: Callable[[], float] = time.monotonic

    async def sleep(self, seconds: float, stop: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, seconds))


class Monitor:
    def __init__(self, schedules: list[Schedule], clock: Clock | None = None) -> None:
        self.schedules = schedules
        self.clock = clock or Clock()
        self.runs: dict[str, int] = {s.name: 0 for s in schedules}

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "monitor started: %s",
            ", ".join(f"{s.name} every {s.interval_seconds:g}s" for s in self.schedules),
        )
        while not stop.is_set():
            now = self.clock.monotonic()
            for schedule in self.schedules:
                if stop.is_set() or schedule.next_run > now:
                    continue
                await self._run_job(schedule)
                # Fixed rate without drift; skip missed runs after a long pause.
                schedule.next_run = max(
                    schedule.next_run + schedule.interval_seconds, self.clock.monotonic()
                )
            next_due = min(s.next_run for s in self.schedules)
            await self.clock.sleep(next_due - self.clock.monotonic(), stop)
        log.info("monitor stopped")

    async def _run_job(self, schedule: Schedule) -> None:
        try:
            await schedule.job(self.clock.wall())
        except Exception:
            log.exception("monitor job %s failed", schedule.name)
        finally:
            self.runs[schedule.name] = self.runs.get(schedule.name, 0) + 1
