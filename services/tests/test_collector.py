import asyncio
import struct
from datetime import UTC, datetime

import psycopg
import pytest
from cadns.collector.dnstap import HIT, MISS
from cadns.collector.framestreams import (
    CONTROL_ACCEPT,
    CONTROL_FINISH,
    CONTROL_READY,
    CONTROL_START,
    CONTROL_STOP,
    DNSTAP_CONTENT_TYPE,
    encode_control,
)
from cadns.collector.service import Batch, Collector, write_batch
from cadns.config import Settings
from conftest import dnstap_frames

T1, T2 = 1_789_000_000.0, 1_789_000_060.0


def test_batch_aggregates_per_name():
    batch = Batch()
    batch.add(HIT, "a.example.org", T2)
    batch.add(HIT, "a.example.org", T1)
    batch.add(MISS, "b.example.org", T1)
    batch.add(MISS, "b.example.org", T2)

    assert batch.hits == {"a.example.org": (2, T2)}
    assert batch.misses == {"b.example.org": T2}
    assert len(batch) == 2


def test_frames_become_batch_entries():
    collector = Collector(None, frozenset(), connect=None)
    for frame in dnstap_frames():
        collector.on_frame(frame)
    collector.on_frame(b"\x80")  # garbage is ignored

    assert set(collector._batch.hits) == {"www.example.test"}
    assert collector._batch.hits["www.example.test"][0] == 2  # A and AAAA
    assert set(collector._batch.misses) == {"example.com", "cadns-nonexistent-fixture-name.com"}


async def rows(conn, sql, *params):
    return await (await conn.execute(sql, params)).fetchall()


@pytest.mark.db
async def test_misses_create_pending_domains_and_queue_them(committed):
    batch = Batch(misses={"new.p4.test": T1})

    queued = await write_batch(committed, batch)

    assert queued == ["new.p4.test"]
    assert await rows(
        committed, "SELECT status, last_queried_at FROM cadns.domains WHERE name = 'new.p4.test'"
    ) == [("pending", datetime.fromtimestamp(T1, UTC))]


@pytest.mark.db
async def test_miss_respects_retry_after_and_keeps_status(committed):
    await committed.execute(
        """INSERT INTO cadns.domains (name, status, retry_after, last_queried_at) VALUES
           ('negative.p4.test', 'nxdomain', now() + interval '10 min', to_timestamp(%s)),
           ('expired.p4.test', 'resolved', now() - interval '1 s', NULL)""",
        (T2,),
    )

    queued = await write_batch(
        committed, Batch(misses={"negative.p4.test": T1, "expired.p4.test": T1})
    )

    assert queued == ["expired.p4.test"]
    assert await rows(
        committed,
        "SELECT name, status, extract(epoch FROM last_queried_at) FROM cadns.domains ORDER BY 1",
    ) == [
        ("expired.p4.test", "resolved", T1),
        ("negative.p4.test", "nxdomain", T2),  # activity never moves backwards
    ]


@pytest.mark.db
async def test_miss_from_before_the_last_measurement_is_not_queued_again(committed):
    """A client retrying while the worker measured must not trigger a second measurement."""
    await committed.execute(
        """INSERT INTO cadns.domains (name, status, measured_at) VALUES
           ('retried.p4.test', 'resolved', to_timestamp(%s)),
           ('expired-later.p4.test', 'resolved', to_timestamp(%s))""",
        (T1 + 0.5, T1 - 30),
    )

    queued = await write_batch(
        committed, Batch(misses={"retried.p4.test": T1, "expired-later.p4.test": T1})
    )

    assert queued == ["expired-later.p4.test"]


@pytest.mark.db
async def test_hits_update_activity_of_known_domains(committed):
    await committed.execute(
        "INSERT INTO cadns.domains (name, status, hit_count) "
        "VALUES ('served.p4.test', 'resolved', 5)"
    )

    await write_batch(
        committed, Batch(hits={"served.p4.test": (3, T2), "unknown.p4.test": (1, T2)})
    )

    assert await rows(
        committed, "SELECT name, hit_count, extract(epoch FROM last_queried_at) FROM cadns.domains"
    ) == [("served.p4.test", 8, T2)]


def data_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


@pytest.mark.db
async def test_collector_end_to_end_over_the_unix_socket(committed, tmp_path):
    """A fake BIND writer sends the recorded frames; the misses end up queued."""
    socket_path = tmp_path / "dnstap.sock"
    collector = Collector(
        socket_path,
        Settings().ignore_suffixes,
        connect=lambda: psycopg.AsyncConnection.connect(autocommit=True),
        flush_seconds=0.1,
    )
    stop = asyncio.Event()
    service = asyncio.create_task(collector.run(stop))
    for _ in range(50):
        if socket_path.exists():
            break
        await asyncio.sleep(0.02)

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(encode_control(CONTROL_READY, (DNSTAP_CONTENT_TYPE,)))
    accept = await reader.readexactly(len(encode_control(CONTROL_ACCEPT, (DNSTAP_CONTENT_TYPE,))))
    writer.write(encode_control(CONTROL_START, (DNSTAP_CONTENT_TYPE,)))
    writer.write(b"".join(data_frame(f) for f in dnstap_frames()))
    writer.write(encode_control(CONTROL_STOP))
    finish = await reader.readexactly(len(encode_control(CONTROL_FINISH)))
    writer.close()

    await asyncio.sleep(0.4)
    stop.set()
    await service

    assert accept == encode_control(CONTROL_ACCEPT, (DNSTAP_CONTENT_TYPE,))
    assert finish == encode_control(CONTROL_FINISH)
    assert await rows(committed, "SELECT domain, reason FROM cadns.measurement_queue") == [
        ("cadns-nonexistent-fixture-name.com", "miss")
    ]
    assert not socket_path.exists()
