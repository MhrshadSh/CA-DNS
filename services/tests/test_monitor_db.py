"""Monitor jobs on a controllable clock: all times are relative to T0."""

from datetime import UTC, datetime, timedelta

import pytest
from cadns.carbon import Signal, WattTimeError
from cadns.monitor.tasks import (
    FreshnessPolicy,
    RetentionPolicy,
    collect_garbage,
    expiring_domains,
    refresh_carbon,
    regions_due,
    scan_expiring,
    slot_start,
)

pytestmark = pytest.mark.db

T0 = datetime(2026, 1, 1, 12, 2, 30, tzinfo=UTC)
POLICY = FreshnessPolicy(activity_window_seconds=3600, lead_seconds=15, min_remeasure_seconds=10)


def ago(**kwargs) -> datetime:
    return T0 - timedelta(**kwargs)


def later(**kwargs) -> datetime:
    return T0 + timedelta(**kwargs)


async def domain(
    tx,
    name,
    *,
    queried=None,
    measured=None,
    retry_after=None,
    first_seen=None,
    a=None,
    aaaa=None,
    address="198.18.1.1",
    address6="2001:db8:88::1",
):
    """A domain whose A/AAAA records (if given) expire at the given times."""
    cur = await tx.execute(
        """INSERT INTO cadns.domains
               (name, status, last_queried_at, measured_at, retry_after, first_seen_at)
           VALUES (%s, 'resolved', %s, %s, %s, coalesce(%s, %s)) RETURNING id""",
        (name, queried, measured, retry_after, first_seen, T0),
    )
    (domain_id,) = await cur.fetchone()
    for rtype, expires, addr in (("A", a, address), ("AAAA", aaaa, address6)):
        if expires is None:
            continue
        await tx.execute(
            "INSERT INTO cadns.endpoints (address) VALUES (%s) ON CONFLICT DO NOTHING", (addr,)
        )
        await tx.execute(
            """INSERT INTO cadns.rrset_records
                   (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
               VALUES (%s, %s, %s, '8.8.8.8', 60, %s, %s)""",
            (
                domain_id,
                rtype,
                addr,
                ago(minutes=5) if expires > ago(minutes=5) else expires,
                expires,
            ),
        )
    return domain_id


# --- expiry scan ---------------------------------------------------------------------------


async def test_expiry_scan_selects_active_domains_about_to_expire(tx):
    await domain(
        tx, "soon.m.test", queried=ago(minutes=1), measured=ago(seconds=50), a=later(seconds=10)
    )
    await domain(
        tx, "expired.m.test", queried=ago(minutes=5), measured=ago(minutes=2), a=ago(seconds=30)
    )
    await domain(
        tx, "later.m.test", queried=ago(minutes=1), measured=ago(seconds=50), a=later(seconds=60)
    )
    await domain(
        tx, "inactive.m.test", queried=ago(hours=2), measured=ago(hours=2), a=later(seconds=5)
    )
    await domain(tx, "never-queried.m.test", measured=ago(minutes=1), a=later(seconds=5))
    await domain(
        tx,
        "backoff.m.test",
        queried=ago(minutes=1),
        retry_after=later(seconds=30),
        a=later(seconds=5),
    )
    await domain(
        tx,
        "just-measured.m.test",
        queried=ago(seconds=1),
        measured=ago(seconds=5),
        a=later(seconds=5),
    )
    await domain(tx, "no-records.m.test", queried=ago(minutes=1), measured=ago(minutes=1))

    assert await expiring_domains(tx, T0, POLICY) == ["expired.m.test", "soon.m.test"]


async def test_domain_expires_with_its_first_expiring_rrset_type(tx):
    """Served while every type has a fresh record (dlz_findzone): AAAA decides here."""
    await domain(
        tx,
        "mixed.m.test",
        queried=ago(minutes=1),
        measured=ago(minutes=1),
        a=later(minutes=10),
        aaaa=later(seconds=10),
    )

    assert await expiring_domains(tx, T0, POLICY) == ["mixed.m.test"]


async def test_scan_enqueues_as_expired_without_touching_queued_misses(tx):
    await domain(
        tx, "a.m.test", queried=ago(minutes=1), measured=ago(minutes=1), a=later(seconds=5)
    )
    await domain(
        tx, "b.m.test", queried=ago(minutes=1), measured=ago(minutes=1), a=later(seconds=5)
    )
    await tx.execute(
        "INSERT INTO cadns.measurement_queue (domain, reason) VALUES ('b.m.test', 'miss')"
    )

    queued = await scan_expiring(tx, T0, POLICY)

    assert queued == ["a.m.test"]
    rows = await (
        await tx.execute(
            "SELECT domain, reason FROM cadns.measurement_queue "
            "WHERE domain LIKE '%%.m.test' ORDER BY 1"
        )
    ).fetchall()
    assert rows == [("a.m.test", "expired"), ("b.m.test", "miss")]


async def test_a_domain_that_keeps_being_queried_is_re_measured_before_every_expiry(tx):
    """Walk the clock forward: each cycle the scan catches the domain before it expires."""
    await domain(tx, "steady.m.test", queried=T0, measured=T0, a=later(seconds=60))
    served_until = later(seconds=60)
    remeasured_at = []

    now = T0
    while now < later(minutes=5):
        now += timedelta(seconds=5)  # scan interval
        await tx.execute(
            "UPDATE cadns.domains SET last_queried_at = %s WHERE name = 'steady.m.test'", (now,)
        )
        assert now < served_until, f"domain stopped being served at {served_until}"
        if await expiring_domains(tx, now, POLICY):
            # The worker re-measures (TTL 60) and replaces the records.
            served_until = now + timedelta(seconds=60)
            remeasured_at.append((now - T0).seconds)
            await tx.execute(
                """UPDATE cadns.rrset_records SET expires_at = %s
                   WHERE domain_id = (SELECT id FROM cadns.domains WHERE name = 'steady.m.test')""",
                (served_until,),
            )
            await tx.execute(
                "UPDATE cadns.domains SET measured_at = %s WHERE name = 'steady.m.test'", (now,)
            )

    assert remeasured_at == [50, 100, 150, 200, 250, 300]


# --- carbon refresh ---------------------------------------------------------------------------


def test_slot_start():
    assert slot_start(T0, 300) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert slot_start(datetime(2026, 1, 1, 12, 5, tzinfo=UTC), 300) == datetime(
        2026, 1, 1, 12, 5, tzinfo=UTC
    )


async def region(tx, code, *points):
    await tx.execute("INSERT INTO cadns.grid_regions (code) VALUES (%s)", (code,))
    for point in points:
        await tx.execute(
            "INSERT INTO cadns.carbon_signals (region_code, point_time, moer_g_per_kwh) "
            "VALUES (%s, %s, 100)",
            (code, point),
        )


async def endpoint_in(tx, address, code, *, anycast=False):
    await tx.execute(
        """INSERT INTO cadns.endpoints (address, region_code, is_anycast) VALUES (%s, %s, %s)
           ON CONFLICT (address) DO UPDATE SET region_code = EXCLUDED.region_code,
                                               is_anycast = EXCLUDED.is_anycast""",
        (address, code, anycast),
    )


async def carbon_world(tx):
    slot = slot_start(T0, 300)
    await region(tx, "M_STALE", slot - timedelta(minutes=5))
    await region(tx, "M_CURRENT", slot)
    await region(tx, "M_NEVER")
    await region(tx, "M_ANYCAST")
    await region(tx, "M_INACTIVE")
    for address, code, anycast in [
        ("198.18.2.1", "M_STALE", False),
        ("198.18.2.2", "M_CURRENT", False),
        ("198.18.2.3", "M_NEVER", False),
        ("198.18.2.4", "M_ANYCAST", True),
    ]:
        await endpoint_in(tx, address, code, anycast=anycast)
        await domain(
            tx,
            f"{code.lower()}.m.test",
            queried=ago(minutes=1),
            a=later(minutes=5),
            address=address,
        )
    await endpoint_in(tx, "198.18.2.5", "M_INACTIVE")
    await domain(
        tx, "inactive.m.test", queried=ago(hours=3), a=later(minutes=5), address="198.18.2.5"
    )


async def test_regions_due_are_active_and_missing_the_current_point(tx):
    await carbon_world(tx)

    assert await regions_due(tx, T0, 3600, 300) == ["M_NEVER", "M_STALE"]


class FakeWattTime:
    def __init__(self, results):
        self.results, self.calls = results, []

    async def current_moer(self, code):
        self.calls.append(code)
        result = self.results[code]
        if isinstance(result, Exception):
            raise result
        return result


async def test_refresh_stores_new_points_and_reports_unavailable_regions(tx):
    await carbon_world(tx)
    new_point = slot_start(T0, 300)
    watttime = FakeWattTime({"M_STALE": Signal("M_STALE", new_point, 321.0), "M_NEVER": None})

    result = await refresh_carbon(tx, watttime, T0, 3600, 300)

    assert result.stored == {"M_STALE": new_point}
    assert result.unavailable == ["M_NEVER"]
    assert await regions_due(tx, T0, 3600, 300) == ["M_NEVER"]


async def test_refresh_skips_backed_off_regions_and_survives_errors(tx):
    await carbon_world(tx)
    watttime = FakeWattTime({"M_STALE": WattTimeError("HTTP 500"), "M_NEVER": None})

    result = await refresh_carbon(tx, watttime, T0, 3600, 300, skip=frozenset({"M_NEVER"}))

    assert watttime.calls == ["M_STALE"]
    assert (result.stored, result.unavailable) == ({}, [])


async def test_unpublished_point_is_fetched_again_next_check(tx):
    """WattTime still returns the previous slot: the region stays due."""
    await carbon_world(tx)
    previous = slot_start(T0, 300) - timedelta(minutes=5)
    watttime = FakeWattTime({"M_STALE": Signal("M_STALE", previous, 300.0), "M_NEVER": None})

    await refresh_carbon(tx, watttime, T0, 3600, 300)

    assert "M_STALE" in await regions_due(tx, T0 + timedelta(minutes=1), 3600, 300)


# --- garbage collection ---------------------------------------------------------------------------


async def rows(tx, sql):
    return await (await tx.execute(sql)).fetchall()


async def test_gc_removes_inactive_domains_and_their_records(tx):
    await domain(tx, "active.m.test", queried=ago(days=1), a=later(minutes=5), address="198.18.3.1")
    await domain(
        tx, "inactive.m.test", queried=ago(days=8), a=later(minutes=5), address="198.18.3.2"
    )
    await domain(tx, "never-queried-old.m.test", first_seen=ago(days=8))
    await domain(tx, "never-queried-new.m.test", first_seen=ago(days=1))

    result = await collect_garbage(tx, T0, RetentionPolicy(domain_days=7, batch_size=1))

    assert result.domains == 2
    assert await rows(
        tx, "SELECT name FROM cadns.domains WHERE name LIKE '%.m.test' ORDER BY 1"
    ) == [
        ("active.m.test",),
        ("never-queried-new.m.test",),
    ]
    assert await rows(
        tx, "SELECT count(*) FROM cadns.rrset_records WHERE address = '198.18.3.2'"
    ) == [(0,)]


async def test_gc_removes_only_old_unreferenced_endpoints(tx):
    await domain(tx, "keeps.m.test", queried=ago(hours=1), a=later(minutes=5), address="198.18.4.1")
    await tx.execute(
        """UPDATE cadns.endpoints SET geo_updated_at = %s WHERE address = '198.18.4.1'""",
        (ago(days=60),),
    )
    await tx.execute(
        """INSERT INTO cadns.endpoints (address, geo_updated_at) VALUES
           ('198.18.4.2', %s), ('198.18.4.3', %s), ('198.18.4.4', NULL)""",
        (ago(days=60), ago(days=1)),
    )

    result = await collect_garbage(tx, T0, RetentionPolicy(endpoint_days=30))

    remaining = await rows(
        tx, "SELECT host(address) FROM cadns.endpoints WHERE address << '198.18.4.0/24' ORDER BY 1"
    )
    assert remaining == [("198.18.4.1",), ("198.18.4.3",)]
    assert result.endpoints >= 2


async def test_gc_trims_carbon_history_but_keeps_each_regions_latest_point(tx):
    await region(tx, "M_OLD", ago(days=40), ago(days=35))
    await region(tx, "M_MIXED", ago(days=40), ago(days=1))

    result = await collect_garbage(tx, T0, RetentionPolicy(carbon_history_days=30))

    assert result.signals == 2
    assert await rows(
        tx,
        "SELECT region_code, count(*) FROM cadns.carbon_signals "
        "WHERE region_code LIKE 'M_%' GROUP BY 1 ORDER BY 1",
    ) == [("M_MIXED", 1), ("M_OLD", 1)]


async def test_gc_can_keep_all_carbon_history(tx):
    await region(tx, "M_OLD", ago(days=400), ago(days=300))

    result = await collect_garbage(tx, T0, RetentionPolicy(carbon_history_days=0))

    assert result.signals == 0


async def test_gc_drops_dead_jobs_of_deleted_domains(tx):
    await tx.execute(
        """INSERT INTO cadns.measurement_queue (domain, reason, state, enqueued_at) VALUES
           ('gone.m.test', 'miss', 'dead', %s), ('recent.m.test', 'miss', 'dead', %s)""",
        (ago(days=8), ago(days=1)),
    )

    result = await collect_garbage(tx, T0, RetentionPolicy(domain_days=7))

    assert result.dead_jobs == 1
