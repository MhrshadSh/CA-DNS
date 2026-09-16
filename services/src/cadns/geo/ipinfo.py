"""IPinfo lookups.

- MmdbSource reads a downloaded IPinfo database (ipinfo_core.mmdb: flat fields
  latitude, longitude, city, country_code, asn "AS15169", is_anycast).
- ApiSource calls the IPinfo API with a Bearer token (never in URLs). Plans
  differ, so it tries https://api.ipinfo.io/lookup/<ip> (nested geo/as objects)
  and falls back to https://ipinfo.io/<ip>/json (flat, "loc" and "org" strings)
  when the account cannot use the first.
- Geolocator tries sources in order and returns the first result that has
  coordinates, which WattTime needs. Databases without an anycast field (the
  IP-to-Geolocation MMDB) can be enriched with one API lookup, because the
  answer policy ranks anycast endpoints as unknown (ADR-5).
"""

import ipaddress
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import httpx
import maxminddb

log = logging.getLogger(__name__)

API_BASE = "https://api.ipinfo.io"
LEGACY_API_BASE = "https://ipinfo.io"


@dataclass(frozen=True)
class Location:
    source: str  # ipinfo_mmdb | ipinfo_api (matches endpoints.geo_source)
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    country: str | None = None
    asn: int | None = None
    is_anycast: bool = False
    # False when the source has no anycast field at all (not "known to be false").
    anycast_known: bool = False

    @property
    def has_coordinates(self) -> bool:
        return self.lat is not None and self.lon is not None


class Source(Protocol):
    async def lookup(self, address: str) -> Location | None: ...


class GeoLookupError(Exception):
    """The source could not answer (network error, quota, ...)."""


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _asn(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.upper().startswith("AS") and value[2:].isdigit():
        return int(value[2:])
    return None


def _bool(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _country(value: Any) -> str | None:
    return value.upper() if isinstance(value, str) and len(value) == 2 else None


def _coordinates(geo: dict[str, Any]) -> tuple[float | None, float | None]:
    lat = _float(geo.get("latitude", geo.get("lat")))
    lon = _float(geo.get("longitude", geo.get("lng")))
    if lat is None and isinstance(geo.get("loc"), str) and "," in geo["loc"]:
        # Legacy API: "37.4056,-122.0775"
        first, _, second = geo["loc"].partition(",")
        lat, lon = _float(first), _float(second)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def location_from_record(record: dict[str, Any], source: str) -> Location:
    """Build a Location from an IPinfo record.

    Handles the MMDB/Core layout (flat or nested "geo"/"as" objects, fields
    latitude/longitude, asn, is_anycast) and the legacy API layout
    ("loc": "lat,lon", "org": "AS15169 Google LLC", "anycast").
    """
    geo = record.get("geo") if isinstance(record.get("geo"), dict) else record
    as_ = record.get("as") if isinstance(record.get("as"), dict) else record
    lat, lon = _coordinates(geo)
    asn = _asn(as_.get("asn"))
    if asn is None and isinstance(record.get("org"), str):
        asn = _asn(record["org"].split(" ", 1)[0])
    anycast = record.get("is_anycast", record.get("anycast"))
    return Location(
        source=source,
        lat=lat,
        lon=lon,
        city=geo.get("city") or None,
        country=_country(geo.get("country_code", geo.get("country"))),
        asn=asn,
        is_anycast=_bool(anycast),
        anycast_known=anycast is not None,
    )


class MmdbSource:
    #: only the Core database carries an anycast field; see lookup()
    provides_anycast = False

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reader = maxminddb.open_database(str(path))

    async def lookup(self, address: str) -> Location | None:
        record = self._reader.get(address)
        if not isinstance(record, dict):
            return None
        return location_from_record(record, "ipinfo_mmdb")

    def close(self) -> None:
        self._reader.close()


class ApiSource:
    #: the API knows whether an address is anycast
    provides_anycast = True

    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client
        self._legacy = False

    async def lookup(self, address: str) -> Location | None:
        legacy = self._legacy
        response = await self._get(address, legacy)
        if response.status_code == 403 and not legacy:
            # Plans without the /lookup endpoint use the legacy one. Retry per
            # call: concurrent lookups may already have flipped the flag.
            if not self._legacy:
                self._legacy = True
                log.info("IPinfo /lookup not available for this plan; using ipinfo.io/<ip>/json")
            response = await self._get(address, legacy=True)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GeoLookupError(f"IPinfo API returned HTTP {response.status_code}")
        record = response.json()
        if record.get("bogon"):
            return None
        return location_from_record(record, "ipinfo_api")

    async def _get(self, address: str, legacy: bool) -> httpx.Response:
        url = f"{LEGACY_API_BASE}/{address}/json" if legacy else f"{API_BASE}/lookup/{address}"
        try:
            return await self._client.get(
                url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GeoLookupError(f"IPinfo API request failed: {exc}") from exc


class Geolocator:
    def __init__(self, sources: list[Source], anycast_lookup: bool = True) -> None:
        self.sources = sources
        self.anycast_lookup = anycast_lookup

    async def locate(self, address: str) -> Location | None:
        """First result with coordinates; otherwise the best partial result."""
        if not ipaddress.ip_address(address).is_global:
            return None
        partial: Location | None = None
        for source in self.sources:
            location = await self._lookup(source, address)
            if location is None:
                continue
            if location.has_coordinates:
                return await self._with_anycast(location, address, source)
            partial = partial or location
        return partial

    async def _with_anycast(self, location: Location, address: str, used: Source) -> Location:
        """Fill in the anycast flag from a source that has one (ADR-5)."""
        if location.anycast_known or not self.anycast_lookup:
            return location
        for source in self.sources:
            if source is used or not getattr(source, "provides_anycast", False):
                continue
            extra = await self._lookup(source, address)
            if extra is not None and extra.anycast_known:
                return replace(
                    location,
                    is_anycast=extra.is_anycast,
                    anycast_known=True,
                    asn=location.asn if location.asn is not None else extra.asn,
                )
        return location

    async def _lookup(self, source: Source, address: str) -> Location | None:
        try:
            return await source.lookup(address)
        except GeoLookupError as exc:
            log.warning("geolocation of %s failed: %s", address, exc)
            return None
