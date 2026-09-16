"""IPinfo lookups.

- MmdbSource reads a downloaded IPinfo database (ipinfo_core.mmdb: flat fields
  latitude, longitude, city, country_code, asn "AS15169", is_anycast).
- ApiSource calls https://api.ipinfo.io/lookup/<ip> (nested geo / as objects),
  authenticated with a Bearer token so the token never appears in URLs.
- Geolocator tries sources in order and returns the first result that has
  coordinates, which WattTime needs.
"""

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import maxminddb

log = logging.getLogger(__name__)

API_BASE = "https://api.ipinfo.io"


@dataclass(frozen=True)
class Location:
    source: str  # ipinfo_mmdb | ipinfo_api (matches endpoints.geo_source)
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    country: str | None = None
    asn: int | None = None
    is_anycast: bool = False

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


def location_from_record(record: dict[str, Any], source: str) -> Location:
    """Build a Location from a flat (MMDB) or nested (API) IPinfo record."""
    geo = record.get("geo") if isinstance(record.get("geo"), dict) else record
    as_ = record.get("as") if isinstance(record.get("as"), dict) else record
    lat = _float(geo.get("latitude", geo.get("lat")))
    lon = _float(geo.get("longitude", geo.get("lng")))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    return Location(
        source=source,
        lat=lat,
        lon=lon,
        city=geo.get("city") or None,
        country=_country(geo.get("country_code")),
        asn=_asn(as_.get("asn")),
        is_anycast=_bool(record.get("is_anycast")),
    )


class MmdbSource:
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
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._client = client

    async def lookup(self, address: str) -> Location | None:
        try:
            response = await self._client.get(
                f"{API_BASE}/lookup/{address}",
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GeoLookupError(f"IPinfo API request failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GeoLookupError(f"IPinfo API returned HTTP {response.status_code}")
        record = response.json()
        if record.get("bogon"):
            return None
        return location_from_record(record, "ipinfo_api")


class Geolocator:
    def __init__(self, sources: list[Source]) -> None:
        self.sources = sources

    async def locate(self, address: str) -> Location | None:
        """First result with coordinates; otherwise the best partial result."""
        if not ipaddress.ip_address(address).is_global:
            return None
        partial: Location | None = None
        for source in self.sources:
            try:
                location = await source.lookup(address)
            except GeoLookupError as exc:
                log.warning("geolocation of %s failed: %s", address, exc)
                continue
            if location is None:
                continue
            if location.has_coordinates:
                return location
            partial = partial or location
        return partial
