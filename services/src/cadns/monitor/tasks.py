"""The monitor's jobs (docs/architecture.md ADR-10).

Every function takes `now` explicitly instead of using the database clock, so
tests can place data at fixed times (a controllable clock).

- expiry scan:    re-enqueue active domains whose served data expires soon
- carbon refresh: fetch the newest MOER point for regions with active endpoints
- garbage collection: remove inactive domains, orphaned endpoints, old signals
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from cadns import queue
from cadns.carbon import WattTimeClient, WattTimeError
from cadns.worker import store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreshnessPolicy:
    activity_window_seconds: int = 3600  # "active" = queried this recently
    lead_seconds: int = 15  # re-measure when served data expires within this
    min_remeasure_seconds: int = 10  # never re-measure more often than this


async def expiring_domains(
    conn: psycopg.AsyncConnection, now: datetime, policy: FreshnessPolicy
) -> list[str]:
    """Active domains whose served data expires within the lead time.

    A domain is served while every stored RRset type has an unexpired record
    (cadns.dlz_findzone), so it stops being served at
    min over types of max(expires_at). Already expired active domains are
    included too (e.g. after the monitor was down).
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """SELECT d.name
               FROM cadns.domains d
               JOIN LATERAL (
                   SELECT min(per_type.expires_at) AS served_until
                   FROM (
                       SELECT max(r.expires_at) AS expires_at
                       FROM cadns.rrset_records r
                       WHERE r.domain_id = d.id
                       GROUP BY r.rtype
                   ) AS per_type
               ) AS s ON s.served_until IS NOT NULL
               WHERE d.last_queried_at > %(now)s - make_interval(secs => %(window)s)
                 AND (d.retry_after IS NULL OR d.retry_after <= %(now)s)
                 AND (d.measured_at IS NULL
                      OR d.measured_at <= %(now)s - make_interval(secs => %(min_interval)s))
                 AND s.served_until < %(now)s + make_interval(secs => %(lead)s)
               ORDER BY s.served_until, d.name""",
            {
                "now": now,
                "window": policy.activity_window_seconds,
                "min_interval": policy.min_remeasure_seconds,
                "lead": policy.lead_seconds,
            },
        )
        return [name for (name,) in await cur.fetchall()]


async def scan_expiring(
    conn: psycopg.AsyncConnection, now: datetime, policy: FreshnessPolicy
) -> list[str]:
    """Enqueue expiring active domains; returns those newly queued."""
    return await queue.enqueue(conn, await expiring_domains(conn, now, policy), "expired")


def slot_start(now: datetime, period_seconds: int) -> datetime:
    """Start of the carbon data period containing `now` (e.g. the 5-minute slot)."""
    epoch = now.timestamp()
    return datetime.fromtimestamp(epoch - epoch % period_seconds, now.tzinfo)


async def regions_due(
    conn: psycopg.AsyncConnection, now: datetime, window_seconds: int, period_seconds: int
) -> list[str]:
    """Regions with active, non-anycast endpoints that lack the current period's signal."""
    async with conn.cursor() as cur:
        await cur.execute(
            """SELECT a.region_code
               FROM (
                   SELECT DISTINCT e.region_code
                   FROM cadns.domains d
                   JOIN cadns.rrset_records r ON r.domain_id = d.id
                   JOIN cadns.endpoints e ON e.address = r.address
                   WHERE d.last_queried_at > %(now)s - make_interval(secs => %(window)s)
                     AND e.region_code IS NOT NULL
                     AND NOT e.is_anycast
               ) AS a
               LEFT JOIN LATERAL (
                   SELECT max(point_time) AS latest FROM cadns.carbon_signals
                   WHERE region_code = a.region_code
               ) AS s ON true
               WHERE s.latest IS NULL OR s.latest < %(slot)s
               ORDER BY a.region_code""",
            {"now": now, "window": window_seconds, "slot": slot_start(now, period_seconds)},
        )
        return [code for (code,) in await cur.fetchall()]


@dataclass(frozen=True)
class CarbonRefresh:
    stored: dict[str, datetime]  # region -> point_time stored
    unavailable: list[str]  # no data or no access for this account


async def refresh_carbon(
    conn: psycopg.AsyncConnection,
    watttime: WattTimeClient,
    now: datetime,
    window_seconds: int,
    period_seconds: int = 300,
    skip: frozenset[str] = frozenset(),
) -> CarbonRefresh:
    """Fetch the current MOER for regions still missing this period's point.

    Meant to run more often than the period: a region is fetched until
    WattTime has published its new point, then left alone until the next period.
    """
    stored: dict[str, datetime] = {}
    unavailable: list[str] = []
    for code in await regions_due(conn, now, window_seconds, period_seconds):
        if code in skip:
            continue
        try:
            signal = await watttime.current_moer(code)
        except WattTimeError as exc:
            log.warning("carbon refresh for %s failed: %s", code, exc)
            continue
        if signal is None:
            unavailable.append(code)
            continue
        await store.save_signal(conn, signal)
        stored[code] = signal.point_time
    return CarbonRefresh(stored, unavailable)


@dataclass(frozen=True)
class RetentionPolicy:
    domain_days: int = 7  # inactive domains
    endpoint_days: int = 30  # endpoints no record refers to
    carbon_history_days: int = 30  # 0 keeps all signals
    batch_size: int = 1000


@dataclass(frozen=True)
class GcResult:
    domains: int = 0
    endpoints: int = 0
    signals: int = 0
    dead_jobs: int = 0


async def _delete_in_batches(conn: psycopg.AsyncConnection, sql: str, params: dict) -> int:
    total = 0
    while True:
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(sql, params)
            deleted = cur.rowcount
        total += deleted
        if deleted < params["limit"]:
            return total


async def collect_garbage(
    conn: psycopg.AsyncConnection, now: datetime, policy: RetentionPolicy
) -> GcResult:
    domain_cutoff = now - timedelta(days=policy.domain_days)
    domains = await _delete_in_batches(
        conn,
        """DELETE FROM cadns.domains WHERE id IN (
               SELECT id FROM cadns.domains
               WHERE coalesce(last_queried_at, first_seen_at) < %(cutoff)s
               LIMIT %(limit)s)""",
        {"cutoff": domain_cutoff, "limit": policy.batch_size},
    )
    # Dead-lettered jobs for names nobody asks for any more.
    dead_jobs = await _delete_in_batches(
        conn,
        """DELETE FROM cadns.measurement_queue WHERE domain IN (
               SELECT q.domain FROM cadns.measurement_queue q
               WHERE q.state = 'dead' AND q.enqueued_at < %(cutoff)s
                 AND NOT EXISTS (SELECT FROM cadns.domains d WHERE d.name = q.domain)
               LIMIT %(limit)s)""",
        {"cutoff": domain_cutoff, "limit": policy.batch_size},
    )
    endpoints = await _delete_in_batches(
        conn,
        """DELETE FROM cadns.endpoints WHERE address IN (
               SELECT e.address FROM cadns.endpoints e
               WHERE coalesce(e.geo_updated_at, '-infinity') < %(cutoff)s
                 AND NOT EXISTS (SELECT FROM cadns.rrset_records r WHERE r.address = e.address)
               LIMIT %(limit)s)""",
        {"cutoff": now - timedelta(days=policy.endpoint_days), "limit": policy.batch_size},
    )
    signals = 0
    if policy.carbon_history_days > 0:
        # Always keep each region's latest point, however old.
        signals = await _delete_in_batches(
            conn,
            """DELETE FROM cadns.carbon_signals WHERE (region_code, point_time) IN (
                   SELECT s.region_code, s.point_time FROM cadns.carbon_signals s
                   WHERE s.point_time < %(cutoff)s
                     AND s.point_time < (SELECT max(point_time) FROM cadns.carbon_signals l
                                         WHERE l.region_code = s.region_code)
                   LIMIT %(limit)s)""",
            {
                "cutoff": now - timedelta(days=policy.carbon_history_days),
                "limit": policy.batch_size,
            },
        )
    return GcResult(domains=domains, endpoints=endpoints, signals=signals, dead_jobs=dead_jobs)
