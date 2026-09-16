"""SQL for the measurement pipeline (schema: db/migrations)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row, dict_row

from cadns.carbon import Region, Signal

PROVIDER = "watttime"


@dataclass
class Endpoint:
    address: str
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    country: str | None = None
    asn: int | None = None
    is_anycast: bool = False
    region_code: str | None = None
    geo_source: str | None = None
    geo_updated_at: datetime | None = None  # None: not geolocated (yet)


@dataclass(frozen=True)
class Record:
    rtype: str
    address: str
    resolver: str
    ttl: int


def location_key(lat: float, lon: float) -> tuple[int, int]:
    """Rounded location (0.01 degree) used to cache region lookups."""
    return round(lat * 100), round(lon * 100)


async def fresh_endpoints(
    conn: psycopg.AsyncConnection, addresses: Iterable[str], max_age_days: int
) -> dict[str, Endpoint]:
    """Endpoints geolocated within max_age_days."""
    async with conn.cursor(row_factory=class_row(Endpoint)) as cur:
        await cur.execute(
            """SELECT host(address) AS address, lat, lon, city, country, asn, is_anycast,
                      region_code, geo_source, geo_updated_at
               FROM cadns.endpoints
               WHERE address = ANY(%s::inet[])
                 AND geo_updated_at > now() - make_interval(days => %s)""",
            (list(addresses), max_age_days),
        )
        return {row.address: row for row in await cur.fetchall()}


async def cached_regions(
    conn: psycopg.AsyncConnection, keys: Iterable[tuple[int, int]], negative_days: int
) -> dict[tuple[int, int], str | None]:
    """Cached region per location; misses and expired negative entries are absent."""
    keys = list(keys)
    if not keys:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            """SELECT lr.lat_e2, lr.lon_e2, lr.region_code
               FROM cadns.location_regions lr
               JOIN unnest(%s::int[], %s::int[]) AS k (lat_e2, lon_e2)
                 ON (lr.lat_e2, lr.lon_e2) = (k.lat_e2, k.lon_e2)
               WHERE lr.provider = %s
                 AND (lr.region_code IS NOT NULL
                      OR lr.looked_up_at > now() - make_interval(days => %s))""",
            ([k[0] for k in keys], [k[1] for k in keys], PROVIDER, negative_days),
        )
        return {(lat, lon): region for lat, lon, region in await cur.fetchall()}


async def save_region(
    conn: psycopg.AsyncConnection, key: tuple[int, int], region: Region | None
) -> None:
    async with conn.transaction():
        if region is not None:
            await conn.execute(
                """INSERT INTO cadns.grid_regions (code, name, provider) VALUES (%s, %s, %s)
                   ON CONFLICT (code)
                   DO UPDATE SET name = coalesce(EXCLUDED.name, grid_regions.name)""",
                (region.code, region.name, PROVIDER),
            )
        await conn.execute(
            """INSERT INTO cadns.location_regions (provider, lat_e2, lon_e2, region_code)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider, lat_e2, lon_e2)
               DO UPDATE SET region_code = EXCLUDED.region_code, looked_up_at = now()""",
            (PROVIDER, key[0], key[1], region.code if region else None),
        )


async def regions_needing_signal(
    conn: psycopg.AsyncConnection, codes: Iterable[str], max_age_seconds: int
) -> set[str]:
    codes = set(codes)
    if not codes:
        return set()
    async with conn.cursor() as cur:
        await cur.execute(
            """SELECT DISTINCT region_code FROM cadns.carbon_signals
               WHERE region_code = ANY(%s)
                 AND fetched_at > now() - make_interval(secs => %s)""",
            (list(codes), max_age_seconds),
        )
        fresh = {row[0] for row in await cur.fetchall()}
    return codes - fresh


async def save_signal(conn: psycopg.AsyncConnection, signal: Signal) -> None:
    async with conn.transaction():
        await conn.execute(
            """INSERT INTO cadns.carbon_signals (region_code, point_time, moer_g_per_kwh)
               VALUES (%s, %s, %s)
               ON CONFLICT (region_code, point_time)
               DO UPDATE SET moer_g_per_kwh = EXCLUDED.moer_g_per_kwh, fetched_at = now()""",
            (signal.region, signal.point_time, signal.moer_g_per_kwh),
        )


async def store_measurement(
    conn: psycopg.AsyncConnection,
    domain: str,
    status: str,
    retry_after_seconds: int | None,
    endpoints: Iterable[Endpoint] = (),
    records: Iterable[Record] = (),
) -> None:
    """Write one measurement atomically.

    resolved:          upsert endpoints, replace the domain's records
    nxdomain / nodata: remove the domain's records (no longer served)
    failed:            keep existing records (they expire on their own)
    """
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO cadns.domains (name, status, measured_at, retry_after)
                   VALUES (%s, %s, now(), now() + make_interval(secs => %s))
                   ON CONFLICT (name) DO UPDATE
                   SET status = EXCLUDED.status, measured_at = EXCLUDED.measured_at,
                       retry_after = EXCLUDED.retry_after
                   RETURNING id""",
            (domain, status, retry_after_seconds),
        )
        (domain_id,) = await cur.fetchone()

        if status == "failed":
            return

        located = [e for e in endpoints if e.geo_updated_at is not None]
        unlocated = [e for e in endpoints if e.geo_updated_at is None]
        if located:
            await cur.executemany(
                """INSERT INTO cadns.endpoints
                           (address, lat, lon, city, country, asn, is_anycast,
                            region_code, geo_source, geo_updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (address) DO UPDATE
                       SET lat = EXCLUDED.lat, lon = EXCLUDED.lon, city = EXCLUDED.city,
                           country = EXCLUDED.country, asn = EXCLUDED.asn,
                           is_anycast = EXCLUDED.is_anycast, region_code = EXCLUDED.region_code,
                           geo_source = EXCLUDED.geo_source,
                           geo_updated_at = EXCLUDED.geo_updated_at""",
                [
                    (
                        e.address,
                        e.lat,
                        e.lon,
                        e.city,
                        e.country,
                        e.asn,
                        e.is_anycast,
                        e.region_code,
                        e.geo_source,
                        e.geo_updated_at,
                    )
                    for e in located
                ],
            )
        if unlocated:
            await cur.executemany(
                "INSERT INTO cadns.endpoints (address) VALUES (%s) ON CONFLICT DO NOTHING",
                [(e.address,) for e in unlocated],
            )

        await cur.execute("DELETE FROM cadns.rrset_records WHERE domain_id = %s", (domain_id,))
        records = list(records)
        if records:
            await cur.executemany(
                """INSERT INTO cadns.rrset_records
                           (domain_id, rtype, address, resolver, ttl, resolved_at, expires_at)
                       VALUES (%s, %s, %s, %s, %s, now(), now() + make_interval(secs => %s))""",
                [(domain_id, r.rtype, r.address, r.resolver, r.ttl, r.ttl) for r in records],
            )


async def endpoint_report(
    conn: psycopg.AsyncConnection, addresses: Iterable[str]
) -> list[dict[str, object]]:
    """Endpoints with their latest MOER, greenest first (for the CLI)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """SELECT host(e.address) AS address, e.city, e.country, e.asn, e.is_anycast,
                      e.region_code, s.moer_g_per_kwh AS moer, s.point_time
               FROM cadns.endpoints e
               LEFT JOIN LATERAL (
                   SELECT moer_g_per_kwh, point_time FROM cadns.carbon_signals
                   WHERE region_code = e.region_code ORDER BY point_time DESC LIMIT 1
               ) s ON true
               WHERE e.address = ANY(%s::inet[])
               ORDER BY family(e.address), s.moer_g_per_kwh ASC NULLS LAST, e.address""",
            (list(addresses),),
        )
        return await cur.fetchall()


async def dlz_answer(conn: psycopg.AsyncConnection, domain: str) -> list[tuple[int, str, str]]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT ttl, type, data FROM cadns.dlz_lookup(%s, '@')", (domain,))
        return await cur.fetchall()
