"""Command-line entry point: `cadns measure <domain>`."""

import argparse
import asyncio
import logging
import sys
from contextlib import AsyncExitStack

import httpx

from cadns import db
from cadns import log as cadns_log
from cadns.carbon import WattTimeClient
from cadns.config import Settings
from cadns.geo import ApiSource, Geolocator, MmdbSource
from cadns.resolvers import ResolverPool
from cadns.worker import store
from cadns.worker.pipeline import Measurement, Pipeline

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
    return Geolocator(sources)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cadns", description="CA-DNS services")
    parser.add_argument("--log-level", default="INFO")
    commands = parser.add_subparsers(dest="command", required=True)
    measure_cmd = commands.add_parser("measure", help="measure one domain now")
    measure_cmd.add_argument("domain")
    args = parser.parse_args(argv)

    cadns_log.setup(args.log_level)
    if args.command == "measure":
        return asyncio.run(measure(args.domain))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
