"""Prometheus metrics (docs/architecture.md ADR-11).

Counters are incremented where the work happens; gauges that describe the
database as a whole are refreshed by the monitor. Each long-running service
serves /metrics on its own port (0 disables it).
"""

import logging
from datetime import datetime

import psycopg
from prometheus_client import Counter, Gauge, start_http_server

log = logging.getLogger(__name__)

# --- counters ---------------------------------------------------------------------
client_responses = Counter(
    "cadns_client_responses_total",
    "Client A/AAAA responses seen on dnstap, by kind",
    ["kind"],  # hit (answered from the database) | miss (recursive)
)
measurements = Counter("cadns_measurements_total", "Finished measurements, by outcome", ["status"])
queue_operations = Counter(
    "cadns_queue_operations_total", "Queue transitions", ["operation"]
)  # enqueued | completed | failed | dead
api_requests = Counter(
    "cadns_api_requests_total", "Requests to external APIs", ["api", "outcome"]
)  # api: ipinfo | watttime; outcome: ok | empty | error

# --- gauges (refreshed from the database by the monitor) -----------------------------
domains_by_status = Gauge("cadns_domains", "Domains by status", ["status"])
served_domains = Gauge("cadns_served_domains", "Domains currently answerable from the database")
queue_depth = Gauge("cadns_queue_depth", "Rows in the measurement queue", ["state"])
queue_oldest_seconds = Gauge("cadns_queue_oldest_seconds", "Age of the oldest pending queue entry")
endpoints_total = Gauge("cadns_endpoints", "Known endpoints", ["located"])
region_moer = Gauge(
    "cadns_region_moer_g_per_kwh", "Latest marginal emission rate per region", ["region"]
)
expected_saving = Gauge(
    "cadns_expected_saving_g_per_kwh",
    "Mean gCO2/kWh avoided per served domain: average candidate MOER minus the chosen one",
)
saving_domains = Gauge(
    "cadns_expected_saving_domains", "Served domains with a choice between known MOER values"
)


def serve(port: int) -> None:
    if port > 0:
        start_http_server(port)
        log.info("serving metrics on port %d", port, extra={"metrics_port": port})


async def refresh_from_database(conn: psycopg.AsyncConnection, now: datetime) -> None:
    """Recompute the gauges that describe stored data."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT status, count(*) FROM cadns.domains GROUP BY status")
        seen = dict(await cur.fetchall())
        for status in ("pending", "resolved", "nxdomain", "nodata", "failed"):
            domains_by_status.labels(status=status).set(seen.get(status, 0))

        await cur.execute(
            """SELECT count(*) FROM cadns.domains d
               WHERE EXISTS (SELECT FROM cadns.rrset_records r WHERE r.domain_id = d.id)
                 AND NOT EXISTS (
                     SELECT FROM cadns.rrset_records r
                     WHERE r.domain_id = d.id
                     GROUP BY r.rtype
                     HAVING max(r.expires_at) <= %s)""",
            (now,),
        )
        served_domains.set((await cur.fetchone())[0])

        await cur.execute("SELECT state, count(*) FROM cadns.measurement_queue GROUP BY state")
        depth = dict(await cur.fetchall())
        for state in ("pending", "dead"):
            queue_depth.labels(state=state).set(depth.get(state, 0))

        await cur.execute(
            """SELECT coalesce(extract(epoch FROM %s - min(enqueued_at)), 0)
               FROM cadns.measurement_queue WHERE state = 'pending'""",
            (now,),
        )
        queue_oldest_seconds.set((await cur.fetchone())[0])

        await cur.execute(
            "SELECT lat IS NOT NULL AS located, count(*) FROM cadns.endpoints GROUP BY 1"
        )
        located = dict(await cur.fetchall())
        endpoints_total.labels(located="yes").set(located.get(True, 0))
        endpoints_total.labels(located="no").set(located.get(False, 0))

        await cur.execute(
            """SELECT region_code, moer FROM (
                   SELECT DISTINCT ON (region_code) region_code, moer_g_per_kwh AS moer
                   FROM cadns.carbon_signals ORDER BY region_code, point_time DESC) latest"""
        )
        for region, moer in await cur.fetchall():
            region_moer.labels(region=region).set(moer)

        # What the greenest choice avoids compared with picking at random from
        # the same candidates, averaged over the domains where it can differ.
        await cur.execute(
            """SELECT count(*), coalesce(avg(mean_moer - best_moer), 0)
               FROM (
                   SELECT avg(m.moer) AS mean_moer, min(m.moer) AS best_moer
                   FROM cadns.domains d
                   JOIN cadns.rrset_records r ON r.domain_id = d.id AND r.expires_at > %s
                   JOIN cadns.endpoints e ON e.address = r.address AND NOT e.is_anycast
                   JOIN LATERAL (
                       SELECT moer_g_per_kwh AS moer FROM cadns.carbon_signals
                       WHERE region_code = e.region_code ORDER BY point_time DESC LIMIT 1
                   ) m ON true
                   GROUP BY d.id, r.rtype
                   HAVING count(DISTINCT m.moer) > 1) per_rrset""",
            (now,),
        )
        count, saving = await cur.fetchone()
        saving_domains.set(count)
        expected_saving.set(float(saving))
