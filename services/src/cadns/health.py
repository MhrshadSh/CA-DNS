"""Liveness heartbeats.

Each long-running service touches a file every time round its main loop; the
container healthcheck fails when that file gets too old (or never appeared).
This catches a wedged loop, which a "process is running" check would not.
"""

import time
from pathlib import Path


class Heartbeat:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def beat(self) -> None:
        self.path.touch()


def age_seconds(path: Path) -> float | None:
    """Seconds since the last beat, or None if the service never started one."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except FileNotFoundError:
        return None
