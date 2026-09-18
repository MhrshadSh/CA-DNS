"""Command-line entry point.

cadns measure <domain>   measure one domain now and print a report
cadns worker             consume the measurement queue (long-running)
cadns collector          turn BIND's dnstap stream into queue entries (long-running)
"""

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import AsyncExitStack

import httpx
import psycopg_pool

from cadns import db, metrics
from cadns import log as cadns_log
from cadns.carbon import WattTimeClient
from cadns.collector.service import Collector
from cadns.config import Settings
from cadns.geo import ApiSource, Geolocator, MmdbSource
from cadns.health import Heartbeat, age_seconds
from cadns.monitor import tasks as monitor_tasks
from cadns.monitor.service import Monitor, Schedule
from cadns.resolvers import ResolverPool
from cadns.worker import store
from cadns.worker.pipeline import API_CONCURRENCY, Measurement, Pipeline
from cadns.worker.service import Worker

log = logging.getLogger("cadns")


def build_geolocator(settings: Settings, http: httpx.AsyncClient) -> Geolocator | None:
    sources = []
    if settings.ipinfo_mmdb is not None and settings.ipinfo_mmdb.is_file():
        sources.append(MmdbSource(settings.ipinfo_mmdb))
    if settings.ipinfo_token is not None:
        sources.append(ApiSource(settings.ipinfo_token.get_secret_value(), http))
    if not sources:
        log.warning("no IPinfo MMDB or token configured: endpoints stay unlocated")
        return None
    return Geolocator(sources, anycast_lookup=settings.anycast_lookup)


def build_watttime(settings: Settings, http: httpx.AsyncClient) -> WattTimeClient | None:
    if settings.watttime_username is None or settings.watttime_password is None:
        log.warning("no WattTime credentials configured: MOER stays unknown")
        return None
    return WattTimeClient(
        settings.watttime_username,
        settings.watttime_password.get_secret_value(),
        http,
        signal_type=settings.watttime_signal_type,
        requests_per_second=settings.watttime_requests_per_second,
    )


async def measure(domain: str) -> int:
    settings = Settings()
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=settings.http_timeout))
        conn = await db.connect(autocommit=True)
        stack.push_async_callback(conn.close)
        pipeline = Pipeline(
            conn,
            settings,
            ResolverPool(settings.resolvers, settings.resolver_timeout),
            build_geolocator(settings, http),
            build_watttime(settings, http),
        )
        result = await pipeline.measure(domain)
        await print_report(conn, result)
    return 0 if result.outcome.status != "failed" else 1


def stop_on_signals() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def run_worker() -> int:
    settings = Settings()
    metrics.serve(settings.metrics_port)
    stop = stop_on_signals()
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=settings.http_timeout))
        pool = psycopg_pool.AsyncConnectionPool(
            "",
            min_size=1,
            max_size=settings.worker_concurrency + 1,
            kwargs={"autocommit": True, "application_name": "cadns-worker"},
            open=False,
        )
        await pool.open(wait=False)
        stack.push_async_callback(pool.close)

        resolvers = ResolverPool(settings.resolvers, settings.resolver_timeout)
        geolocator = build_geolocator(settings, http)
        watttime = build_watttime(settings, http)
        api_limit = asyncio.Semaphore(API_CONCURRENCY)

        async def measure_domain(conn, domain):
            pipeline = Pipeline(conn, settings, resolvers, geolocator, watttime, api_limit)
            return await pipeline.measure(domain)

        worker = Worker(
            pool,
            measure_domain,
            settings.queue_policy,
            concurrency=settings.worker_concurrency,
            poll_seconds=settings.queue_poll_seconds,
            grace_seconds=settings.worker_grace_seconds,
            listen=lambda: db.connect(autocommit=True),
            heartbeat=Heartbeat(settings.health_dir / "worker.alive"),
        )
        await worker.run(stop)
    return 0


async def run_collector() -> int:
    settings = Settings()
    metrics.serve(settings.metrics_port)
    collector = Collector(
        settings.dnstap_socket,
        settings.ignore_suffixes,
        connect=lambda: db.connect(autocommit=True),
        flush_seconds=settings.collector_flush_seconds,
        max_batch=settings.collector_max_batch,
        heartbeat=Heartbeat(settings.health_dir / "collector.alive"),
    )
    await collector.run(stop_on_signals())
    return 0


async def run_monitor() -> int:
    settings = Settings()
    metrics.serve(settings.metrics_port)
    stop = stop_on_signals()
    freshness = monitor_tasks.FreshnessPolicy(
        activity_window_seconds=settings.monitor_activity_window_seconds,
        lead_seconds=settings.monitor_lead_seconds,
        min_remeasure_seconds=settings.monitor_min_remeasure_seconds,
    )
    retention = monitor_tasks.RetentionPolicy(
        domain_days=settings.gc_domain_retention_days,
        endpoint_days=settings.gc_endpoint_retention_days,
        carbon_history_days=settings.gc_carbon_history_days,
    )
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=settings.http_timeout))
        pool = psycopg_pool.AsyncConnectionPool(
            "",
            min_size=1,
            max_size=2,
            kwargs={"autocommit": True, "application_name": "cadns-monitor"},
            open=False,
        )
        await pool.open(wait=False)
        stack.push_async_callback(pool.close)
        watttime = build_watttime(settings, http)

        async def expiry_scan(now):
            async with pool.connection() as conn:
                queued = await monitor_tasks.scan_expiring(conn, now, freshness)
            if queued:
                log.info(
                    "re-measuring %d expiring domain(s): %s", len(queued), ", ".join(queued[:5])
                )

        unavailable_until: dict[str, float] = {}

        async def carbon_refresh(now):
            loop_time = asyncio.get_running_loop().time()
            skip = frozenset(r for r, until in unavailable_until.items() if until > loop_time)
            async with pool.connection() as conn:
                result = await monitor_tasks.refresh_carbon(
                    conn,
                    watttime,
                    now,
                    settings.monitor_activity_window_seconds,
                    settings.carbon_period_seconds,
                    skip,
                )
            for region in result.unavailable:
                unavailable_until[region] = loop_time + settings.carbon_unavailable_backoff_seconds
            for region, point_time in result.stored.items():
                log.info("carbon signal %s at %s", region, point_time.isoformat())

        async def garbage_collection(now):
            async with pool.connection() as conn:
                result = await monitor_tasks.collect_garbage(conn, now, retention)
            if any(vars(result).values()):
                log.info("garbage collected: %s", result)

        async def refresh_metrics(now):
            async with pool.connection() as conn:
                await metrics.refresh_from_database(conn, now)

        schedules = [
            Schedule("expiry-scan", settings.monitor_scan_seconds, expiry_scan),
            Schedule("metrics", settings.metrics_refresh_seconds, refresh_metrics),
        ]
        if watttime is not None:
            schedules.append(
                Schedule("carbon-refresh", settings.monitor_carbon_check_seconds, carbon_refresh)
            )
        schedules.append(Schedule("gc", settings.monitor_gc_seconds, garbage_collection))
        await Monitor(schedules, heartbeat=Heartbeat(settings.health_dir / "monitor.alive")).run(
            stop
        )
    return 0


async def print_report(conn, result: Measurement) -> None:
    out = sys.stdout.write
    out(f"\n{result.domain}: {result.outcome.status}")
    if result.outcome.retry_after_seconds is not None:
        out(f" (retry after {result.outcome.retry_after_seconds}s)")
    out("\n\nUpstream answers\n")
    for a in sorted(result.outcome.answers, key=lambda a: (a.rtype, a.resolver)):
        detail = ", ".join(a.addresses) if a.addresses else "-"
        ttl = f"ttl={a.ttl}" if a.ttl is not None else ""
        out(f"  {a.rtype:<4} @{a.resolver:<15} {a.status:<9} {ttl:<9} {detail}\n")

    if result.outcome.addresses:
        out("\nEndpoints (greenest first)\n")
        for e in await store.endpoint_report(conn, result.outcome.addresses):
            where = ", ".join(str(x) for x in (e["city"], e["country"]) if x) or "unlocated"
            asn = f"AS{e['asn']}" if e["asn"] else ""
            moer = f"{e['moer']:.0f} gCO2/kWh" if e["moer"] is not None else "MOER unknown"
            flags = " anycast" if e["is_anycast"] else ""
            out(
                f"  {e['address']:<39} {where:<28} {asn:<9} "
                f"{e['region_code'] or '-':<14} {moer}{flags}\n"
            )

    answer = await store.dlz_answer(conn, result.domain)
    out("\nCA-DNS answer (cadns.dlz_lookup)\n")
    served = [row for row in answer if row[1] in ("A", "AAAA")]
    if not served:
        out("  not served (clients get the normal recursive answer)\n")
    for ttl, rtype, data in served:
        out(f"  {rtype:<4} {data} ttl={ttl}\n")


def healthcheck(component: str, max_age: float) -> int:
    """Exit 0 while the service is looping, 1 once its heartbeat goes stale."""
    path = Settings().health_dir / f"{component}.alive"
    age = age_seconds(path)
    if age is None:
        print(f"{component}: no heartbeat yet ({path})", file=sys.stderr)
        return 1
    if age > max_age:
        print(f"{component}: last heartbeat {age:.0f}s ago, limit {max_age:.0f}s", file=sys.stderr)
        return 1
    print(f"{component}: alive, last heartbeat {age:.0f}s ago")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cadns", description="CA-DNS services")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--log-format", choices=["json", "text"], default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    measure_cmd = commands.add_parser("measure", help="measure one domain now")
    measure_cmd.add_argument("domain")
    commands.add_parser("worker", help="consume the measurement queue")
    commands.add_parser("collector", help="read dnstap and enqueue misses")
    commands.add_parser("monitor", help="keep active domains and carbon signals fresh")
    health_cmd = commands.add_parser("healthcheck", help="liveness probe (container healthcheck)")
    health_cmd.add_argument("component", choices=["collector", "worker", "monitor"])
    health_cmd.add_argument("--max-age", type=float, default=60.0, help="seconds (default 60)")
    args = parser.parse_args(argv)

    settings = Settings()
    # Humans run `measure`; the long-running services log JSON for collection.
    fmt = args.log_format or ("text" if args.command == "measure" else settings.log_format)
    cadns_log.setup(args.log_level or settings.log_level, fmt, service=args.command)
    if args.command == "measure":
        return asyncio.run(measure(args.domain))
    if args.command == "worker":
        return asyncio.run(run_worker())
    if args.command == "collector":
        return asyncio.run(run_collector())
    if args.command == "monitor":
        return asyncio.run(run_monitor())
    if args.command == "healthcheck":
        return healthcheck(args.component, args.max_age)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
