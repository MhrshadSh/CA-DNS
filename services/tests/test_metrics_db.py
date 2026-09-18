"""Database-derived gauges (the monitor refreshes these)."""

from datetime import UTC, datetime, timedelta

import pytest
from cadns import metrics
from prometheus_client import REGISTRY

pytestmark = pytest.mark.db

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def value(name, **labels):
    return REGISTRY.get_sample_value(name, labels or None)


async def domain_with(tx, name, entries, *, expires=None, status="resolved"):
    """entries: (address, region, moer) - region None leaves the MOER unknown."""
    cur = await tx.execute(
        "INSERT INTO cadns.domains (name, status) VALUES (%s, %s) RETURNING id", (name, status)
    )
    (domain_id,) = await cur.fetchone()
    for address, region, moer in entries:
        if region is not None:
            await tx.execute(
                "INSERT INTO cadns.grid_regions (code) VALUES (%s) ON CONFLICT DO NOTHING",
                (region,),
            )
            await tx.execute(
                """INSERT INTO cadns.carbon_signals (region_code, point_time, moer_g_per_kwh)
                   VALUES (%s, %s, %s) ON CONFLICT (region_code, point_time)
                   DO UPDATE SET moer_g_per_kwh = EXCLUDED.moer_g_per_kwh""",
                (region, NOW - timedelta(minutes=1), moer),
            )
        await tx.execute(
            """INSERT INTO cadns.endpoints (address, lat, lon, region_code) VALUES (%s, 1, 1, %s)
               ON CONFLICT (address) DO UPDATE SET region_code = EXCLUDED.region_code""",
            (address, region),
        )
        expires_at = expires or NOW + timedelta(minutes=5)
        await tx.execute(
            """INSERT INTO cadns.rrset_records
                   (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
               VALUES (%s, 'A', %s, '8.8.8.8', 300, %s, %s)""",
            (domain_id, address, min(NOW, expires_at) - timedelta(minutes=5), expires_at),
        )
    return domain_id


async def test_gauges_describe_the_stored_data(tx):
    await domain_with(
        tx, "green.m6.test", [("198.18.9.1", "M6_LOW", 100), ("198.18.9.2", "M6_HIGH", 500)]
    )
    await domain_with(
        tx, "expired.m6.test", [("198.18.9.3", "M6_LOW", 100)], expires=NOW - timedelta(minutes=1)
    )
    await domain_with(tx, "nx.m6.test", [], status="nxdomain")
    await tx.execute(
        "INSERT INTO cadns.measurement_queue (domain, reason, enqueued_at) VALUES "
        "('a.m6.test', 'miss', %s), ('b.m6.test', 'expired', %s)",
        (NOW - timedelta(seconds=30), NOW - timedelta(seconds=90)),
    )

    await metrics.refresh_from_database(tx, NOW)

    assert value("cadns_served_domains") == 1  # only the unexpired one
    assert value("cadns_domains", status="nxdomain") == 1
    assert value("cadns_queue_depth", state="pending") == 2
    assert value("cadns_queue_depth", state="dead") == 0
    assert value("cadns_queue_oldest_seconds") == 90
    assert value("cadns_region_moer_g_per_kwh", region="M6_LOW") == 100


async def test_expected_saving_is_the_gap_to_the_average_candidate(tx):
    # Candidates at 100 and 500: picking the greenest avoids 300 - 100 = 200 g/kWh.
    await domain_with(
        tx, "choice.m6.test", [("198.18.9.4", "M6_LOW", 100), ("198.18.9.5", "M6_HIGH", 500)]
    )
    # Only one region: nothing to choose, so it must not count.
    await domain_with(tx, "single.m6.test", [("198.18.9.6", "M6_LOW", 100)])

    await metrics.refresh_from_database(tx, NOW)

    assert value("cadns_expected_saving_domains") == 1
    assert value("cadns_expected_saving_g_per_kwh") == 200
