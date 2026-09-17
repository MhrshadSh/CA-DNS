"""Pipeline against the real schema, with fake DNS, IPinfo and WattTime.

Runs as cadns_app (the services' role) inside a transaction that is rolled
back, so it also checks that role's privileges. Uses the benchmarking prefix
198.18.0.0/15 to stay clear of demo seed data.
"""

import os
from datetime import UTC, datetime

import psycopg
import pytest
from cadns.carbon import Region, Signal
from cadns.geo import Location
from cadns.resolvers import Answer
from cadns.worker import store
from cadns.worker.pipeline import Pipeline

pytestmark = pytest.mark.db

DOMAIN = "www.pipeline.test"
PARIS, VIRGINIA, SYDNEY = (48.8566, 2.3522), (38.9072, -77.0369), (-33.8688, 151.2093)


@pytest.fixture
async def conn():
    if "PGHOST" not in os.environ:
        pytest.skip("no database configured (run with make test)")
    connection = await psycopg.AsyncConnection.connect()
    async with connection, connection.transaction(force_rollback=True):
        yield connection


class FakePool:
    def __init__(self, answers):
        self.answers = answers

    async def resolve(self, qname):
        return self.answers


class FakeGeo:
    def __init__(self, locations):
        self.locations, self.calls = locations, []

    async def locate(self, address):
        self.calls.append(address)
        return self.locations.get(address)


class FakeWattTime:
    def __init__(self, regions, moer):
        self.regions, self.moer = regions, moer
        self.region_calls, self.moer_calls = [], []

    async def region_for(self, lat, lon):
        self.region_calls.append((lat, lon))
        return self.regions.get(store.location_key(lat, lon))

    async def current_moer(self, region):
        self.moer_calls.append(region)
        if region not in self.moer:
            return None
        return Signal(region, datetime(2026, 9, 15, 12, 0, tzinfo=UTC), self.moer[region])


def located(lat_lon, *, anycast=False):
    return Location("ipinfo_api", lat=lat_lon[0], lon=lat_lon[1], country="XX", is_anycast=anycast)


def answers(a=(), aaaa=(), resolver="8.8.8.8", ttl=300):
    return [
        Answer(resolver, "A", "noerror", tuple(a), ttl if a else None),
        Answer(resolver, "AAAA", "noerror", tuple(aaaa), ttl if aaaa else None),
    ]


def world():
    geo = FakeGeo(
        {
            "198.18.0.1": located(VIRGINIA),
            "198.18.0.2": located(PARIS),
            "198.18.0.3": located(PARIS),
            "2001:db8:77::1": located(SYDNEY),
            "2001:db8:77::2": located(PARIS),
        }
    )
    watttime = FakeWattTime(
        regions={
            store.location_key(*PARIS): Region("T_PIPE_FR", "France"),
            store.location_key(*VIRGINIA): Region("T_PIPE_PJM", "PJM"),
            store.location_key(*SYDNEY): Region("T_PIPE_NSW", "NSW"),
        },
        moer={"T_PIPE_FR": 60.0, "T_PIPE_PJM": 750.0, "T_PIPE_NSW": 900.0},
    )
    return geo, watttime


async def fetch(conn, sql, *params):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def test_resolved_domain_is_stored_and_served_greenest(conn, settings):
    geo, watttime = world()
    pool = FakePool(
        answers(["198.18.0.1", "198.18.0.2"], ["2001:db8:77::1", "2001:db8:77::2"])
        + answers(["198.18.0.2", "198.18.0.3"], [], resolver="1.1.1.1", ttl=120)
    )

    result = await Pipeline(conn, settings, pool, geo, watttime).measure("WWW.Pipeline.TEST.")

    assert result.domain == DOMAIN
    assert result.outcome.status == "resolved"
    rows = await fetch(
        conn,
        """SELECT rtype, host(address), host(resolver), ttl FROM cadns.rrset_records r
           JOIN cadns.domains d ON d.id = r.domain_id WHERE d.name = %s ORDER BY 1, 2, 3""",
        DOMAIN,
    )
    assert rows == [
        ("A", "198.18.0.1", "8.8.8.8", 300),
        ("A", "198.18.0.2", "1.1.1.1", 120),
        ("A", "198.18.0.2", "8.8.8.8", 300),
        ("A", "198.18.0.3", "1.1.1.1", 120),
        ("AAAA", "2001:db8:77::1", "8.8.8.8", 300),
        ("AAAA", "2001:db8:77::2", "8.8.8.8", 300),
    ]
    assert await fetch(
        conn, "SELECT status, retry_after FROM cadns.domains WHERE name = %s", DOMAIN
    ) == [("resolved", None)]
    # Two Paris endpoints share one region lookup.
    assert len(watttime.region_calls) == 3
    assert sorted(watttime.moer_calls) == ["T_PIPE_FR", "T_PIPE_NSW", "T_PIPE_PJM"]

    answer = await store.dlz_answer(conn, DOMAIN)
    served = {(rtype, data) for _, rtype, data in answer if rtype in ("A", "AAAA")}
    assert ("AAAA", "2001:db8:77::2") in served
    assert served & {("A", "198.18.0.2"), ("A", "198.18.0.3")}
    assert ("A", "198.18.0.1") not in served


async def test_second_measurement_uses_cached_geo_regions_and_signals(conn, settings):
    geo, watttime = world()
    pool = FakePool(answers(["198.18.0.1", "198.18.0.2"], ["2001:db8:77::2"]))
    pipeline = Pipeline(conn, settings, pool, geo, watttime)

    await pipeline.measure(DOMAIN)
    geo.calls.clear()
    watttime.region_calls.clear()
    watttime.moer_calls.clear()
    await pipeline.measure(DOMAIN)

    assert geo.calls == []
    assert watttime.region_calls == []
    assert watttime.moer_calls == []  # signals fetched < carbon_max_age ago


async def test_anycast_endpoint_gets_no_region(conn, settings):
    geo, watttime = world()
    geo.locations["198.18.0.9"] = located(VIRGINIA, anycast=True)
    pool = FakePool(answers(["198.18.0.9"], ["2001:db8:77::2"]))

    await Pipeline(conn, settings, pool, geo, watttime).measure(DOMAIN)

    assert await fetch(
        conn,
        "SELECT is_anycast, region_code FROM cadns.endpoints WHERE address = '198.18.0.9'",
    ) == [(True, None)]


async def test_negative_result_removes_served_records(conn, settings):
    geo, watttime = world()
    await Pipeline(
        conn, settings, FakePool(answers(["198.18.0.1"], ["2001:db8:77::1"])), geo, watttime
    ).measure(DOMAIN)

    gone = [Answer("8.8.8.8", t, "nxdomain", negative_ttl=900) for t in ("A", "AAAA")]
    await Pipeline(conn, settings, FakePool(gone), geo, watttime).measure(DOMAIN)

    assert await fetch(
        conn,
        """SELECT status, retry_after > now() + interval '899 s',
                  (SELECT count(*) FROM cadns.rrset_records WHERE domain_id = d.id)
           FROM cadns.domains d WHERE name = %s""",
        DOMAIN,
    ) == [("nxdomain", True, 0)]
    assert await store.dlz_answer(conn, DOMAIN) == []


async def test_failed_measurement_keeps_existing_records(conn, settings):
    geo, watttime = world()
    await Pipeline(
        conn, settings, FakePool(answers(["198.18.0.1"], ["2001:db8:77::1"])), geo, watttime
    ).measure(DOMAIN)

    broken = [Answer("8.8.8.8", "A", "timeout"), Answer("8.8.8.8", "AAAA", "servfail")]
    result = await Pipeline(conn, settings, FakePool(broken), geo, watttime).measure(DOMAIN)

    assert result.outcome.status == "failed"
    assert await fetch(
        conn,
        """SELECT status, (SELECT count(*) FROM cadns.rrset_records WHERE domain_id = d.id)
           FROM cadns.domains d WHERE name = %s""",
        DOMAIN,
    ) == [("failed", 2)]


async def test_location_outside_coverage_is_cached(conn, settings):
    geo, watttime = world()
    nowhere = (-54.8, -68.3)
    geo.locations["198.18.0.7"] = located(nowhere)
    pool = FakePool(answers(["198.18.0.7"], []))
    pipeline = Pipeline(conn, settings, pool, geo, watttime)

    await pipeline.measure(DOMAIN)
    await pipeline.measure("other.pipeline.test")

    assert watttime.region_calls == [(-54.8, -68.3)]
    assert await fetch(
        conn,
        "SELECT region_code FROM cadns.location_regions WHERE (lat_e2, lon_e2) = (%s, %s)",
        *store.location_key(*nowhere),
    ) == [(None,)]


async def test_region_without_carbon_access_is_served_as_unknown(conn, settings):
    geo, watttime = world()
    del watttime.moer["T_PIPE_PJM"]
    pool = FakePool(answers(["198.18.0.1"], []))

    await Pipeline(conn, settings, pool, geo, watttime).measure(DOMAIN)

    report = await store.endpoint_report(conn, ["198.18.0.1"])
    assert report[0]["region_code"] == "T_PIPE_PJM"
    assert report[0]["moer"] is None
    assert [data for _, t, data in await store.dlz_answer(conn, DOMAIN) if t == "A"] == [
        "198.18.0.1"
    ]


async def test_without_geolocation_or_carbon_endpoints_are_unlocated(conn, settings):
    pool = FakePool(answers(["198.18.0.5"], ["2001:db8:77::5"]))

    await Pipeline(conn, settings, pool).measure(DOMAIN)

    assert await fetch(
        conn,
        """SELECT host(address), lat, geo_updated_at FROM cadns.endpoints
           WHERE address IN ('198.18.0.5', '2001:db8:77::5') ORDER BY 1""",
    ) == [("198.18.0.5", None, None), ("2001:db8:77::5", None, None)]
    assert len(await store.dlz_answer(conn, DOMAIN)) == 4  # SOA, NS, A, AAAA


async def test_min_record_ttl_extends_expiry_but_keeps_the_received_ttl(conn, settings):
    await conn.execute("UPDATE cadns.settings SET min_record_ttl = 60")
    pool = FakePool(answers(["198.18.0.1"], ["2001:db8:77::1"], ttl=20))

    await Pipeline(conn, settings, pool).measure(DOMAIN)

    assert await fetch(
        conn,
        """SELECT DISTINCT ttl, extract(epoch FROM expires_at - resolved_at)::int
           FROM cadns.rrset_records r JOIN cadns.domains d ON d.id = r.domain_id
           WHERE d.name = %s""",
        DOMAIN,
    ) == [(20, 60)]
