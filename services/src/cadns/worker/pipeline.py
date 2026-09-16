"""Measurement pipeline (docs/architecture.md ADR-9).

    resolve (all resolvers, A + AAAA)
      -> classify: resolved | nxdomain | nodata | failed
      -> geolocate endpoints not geolocated recently (IPinfo)
      -> grid region per location (cached permanently)
      -> current MOER per region (refreshed when older than carbon_max_age)
      -> store the domain, endpoints and records in one transaction

Region lookups and carbon signals are shared caches and are committed as soon
as they are fetched; the per-domain write is atomic.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import dns.exception
import dns.name
import psycopg

from cadns.carbon import WattTimeClient, WattTimeError
from cadns.config import Settings
from cadns.geo import Geolocator
from cadns.resolvers import Answer, ResolverPool
from cadns.worker import store
from cadns.worker.store import Endpoint, Record

log = logging.getLogger(__name__)

API_CONCURRENCY = 8


@dataclass(frozen=True)
class Outcome:
    status: str  # resolved | nxdomain | nodata | failed
    answers: tuple[Answer, ...]
    retry_after_seconds: int | None = None

    @property
    def records(self) -> list[Record]:
        return [
            Record(a.rtype, address, a.resolver, a.ttl or 0)
            for a in self.answers
            for address in a.addresses
        ]

    @property
    def addresses(self) -> set[str]:
        return {address for a in self.answers for address in a.addresses}


@dataclass
class Measurement:
    domain: str
    outcome: Outcome
    endpoints: dict[str, Endpoint] = field(default_factory=dict)


def normalize_domain(domain: str) -> str:
    """Canonical form stored in cadns.domains: lowercase, IDNA, no trailing dot."""
    try:
        name = dns.name.from_unicode(domain.strip())
    except dns.exception.DNSException as exc:
        raise ValueError(f"not a domain name: {domain!r}") from exc
    text = name.to_text(omit_final_dot=True).lower()
    if text in ("", ".", "@"):
        raise ValueError(f"not a domain name: {domain!r}")
    return text


def classify(answers: list[Answer], settings: Settings) -> Outcome:
    """Combine all resolver answers into one outcome.

    Records are only replaced when every type (A and AAAA) got at least one
    definitive answer. Otherwise a missing AAAA could be served as NODATA.
    """
    answers_t = tuple(answers)
    for rtype in {a.rtype for a in answers}:
        if not any(a.definitive for a in answers if a.rtype == rtype):
            return Outcome("failed", answers_t, settings.failed_retry_seconds)

    if any(a.addresses for a in answers):
        return Outcome("resolved", answers_t)

    negative_ttls = [a.negative_ttl for a in answers if a.negative_ttl is not None]
    backoff = min(negative_ttls) if negative_ttls else settings.negative_min_seconds
    backoff = max(settings.negative_min_seconds, min(settings.negative_max_seconds, backoff))
    status = "nxdomain" if any(a.status == "nxdomain" for a in answers) else "nodata"
    return Outcome(status, answers_t, backoff)


class Pipeline:
    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        settings: Settings,
        pool: ResolverPool,
        geolocator: Geolocator | None = None,
        watttime: WattTimeClient | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.pool = pool
        self.geolocator = geolocator
        self.watttime = watttime
        self._api = asyncio.Semaphore(API_CONCURRENCY)

    async def measure(self, domain: str) -> Measurement:
        name = normalize_domain(domain)
        outcome = classify(await self.pool.resolve(name), self.settings)
        measurement = Measurement(name, outcome)

        if outcome.status == "resolved":
            measurement.endpoints = await self._endpoints(outcome.addresses)
            await self._refresh_signals(measurement.endpoints.values())

        await store.store_measurement(
            self.conn,
            name,
            outcome.status,
            outcome.retry_after_seconds,
            measurement.endpoints.values(),
            outcome.records,
        )
        log.info("measured %s: %s (%d addresses)", name, outcome.status, len(outcome.addresses))
        return measurement

    async def _endpoints(self, addresses: set[str]) -> dict[str, Endpoint]:
        endpoints = await store.fresh_endpoints(
            self.conn, addresses, self.settings.geo_max_age_days
        )
        missing = sorted(addresses - endpoints.keys())
        located = await asyncio.gather(*(self._locate(a) for a in missing))
        endpoints.update({e.address: e for e in located})
        await self._assign_regions(endpoints.values())
        return endpoints

    async def _locate(self, address: str) -> Endpoint:
        if self.geolocator is None:
            return Endpoint(address)
        async with self._api:
            location = await self.geolocator.locate(address)
        if location is None:
            return Endpoint(address)
        return Endpoint(
            address=address,
            lat=location.lat,
            lon=location.lon,
            city=location.city,
            country=location.country,
            asn=location.asn,
            is_anycast=location.is_anycast,
            geo_source=location.source,
            geo_updated_at=datetime.now(UTC),
        )

    async def _assign_regions(self, endpoints) -> None:
        """Region per endpoint from its rounded location (anycast stays unknown)."""
        wanted = [e for e in endpoints if e.lat is not None and not e.is_anycast]
        keys = {store.location_key(e.lat, e.lon) for e in wanted}
        regions = await store.cached_regions(self.conn, keys, self.settings.region_negative_days)

        if self.watttime is not None:
            unknown = sorted(keys - regions.keys())
            results = await asyncio.gather(*(self._region(k) for k in unknown))
            for key, (ok, region) in zip(unknown, results, strict=True):
                if ok:
                    await store.save_region(self.conn, key, region)
                    regions[key] = region.code if region else None

        for e in wanted:
            e.region_code = regions.get(store.location_key(e.lat, e.lon), e.region_code)

    async def _region(self, key: tuple[int, int]):
        async with self._api:
            try:
                return True, await self.watttime.region_for(key[0] / 100, key[1] / 100)
            except WattTimeError as exc:
                log.warning("region lookup for %s failed: %s", key, exc)
                return False, None

    async def _refresh_signals(self, endpoints) -> None:
        if self.watttime is None:
            return
        codes = {e.region_code for e in endpoints if e.region_code}
        stale = sorted(
            await store.regions_needing_signal(
                self.conn, codes, self.settings.carbon_max_age_seconds
            )
        )

        async def fetch(code: str):
            async with self._api:
                try:
                    return await self.watttime.current_moer(code)
                except WattTimeError as exc:
                    log.warning("MOER for %s failed: %s", code, exc)
                    return None

        for signal in await asyncio.gather(*(fetch(c) for c in stale)):
            if signal is not None:
                await store.save_signal(self.conn, signal)
