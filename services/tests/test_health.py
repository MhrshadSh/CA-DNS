import os
import time

from cadns.health import Heartbeat, age_seconds


def test_no_heartbeat_yet(tmp_path):
    assert age_seconds(tmp_path / "worker.alive") is None


def test_beat_creates_the_file_and_its_directory(tmp_path):
    heartbeat = Heartbeat(tmp_path / "nested" / "worker.alive")

    heartbeat.beat()

    assert age_seconds(heartbeat.path) < 5


def test_age_grows_with_the_file_mtime(tmp_path):
    heartbeat = Heartbeat(tmp_path / "worker.alive")
    heartbeat.beat()
    stale = time.time() - 120
    os.utime(heartbeat.path, (stale, stale))

    assert 115 < age_seconds(heartbeat.path) < 125

    heartbeat.beat()
    assert age_seconds(heartbeat.path) < 5
