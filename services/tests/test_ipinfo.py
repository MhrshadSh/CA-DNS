import asyncio

import httpx
import pytest
from cadns.geo import ApiSource, Geolocator, Location
from cadns.geo.ipinfo import location_from_record
from conftest import api_fixture


def api(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_api_lookup_parses_core_response_with_bearer_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=api_fixture("ipinfo_lookup_8.8.8.8"))

    async with api(handler) as client:
        location = await ApiSource("secret-token", client).lookup("8.8.8.8")

    assert location == Location(
        source="ipinfo_api",
        lat=37.4056,
        lon=-122.0775,
        city="Mountain View",
        country="US",
        asn=15169,
        is_anycast=True,
        anycast_known=True,
    )
    assert str(seen[0].url) == "https://api.ipinfo.io/lookup/8.8.8.8"
    assert seen[0].headers["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(seen[0].url)


async def test_api_not_found_and_bogon_are_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("192.0.2.1"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"ip": "10.0.0.1", "bogon": True})

    async with api(handler) as client:
        source = ApiSource("t", client)
        assert await source.lookup("192.0.2.1") is None
        assert await source.lookup("10.0.0.1") is None


async def test_api_falls_back_to_legacy_endpoint_on_403():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.ipinfo.io":
            return httpx.Response(403, json={"error": "not available on your plan"})
        return httpx.Response(200, json=api_fixture("ipinfo_legacy_8.8.8.8"))

    async with api(handler) as client:
        source = ApiSource("t", client)
        first = await source.lookup("8.8.8.8")
        await source.lookup("1.1.1.1")  # remembers the working endpoint

    assert first.lat == 37.4056
    assert first.asn == 15169  # parsed from "AS15169 Google LLC"
    assert first.is_anycast
    assert first.anycast_known
    assert seen == [
        "https://api.ipinfo.io/lookup/8.8.8.8",
        "https://ipinfo.io/8.8.8.8/json",
        "https://ipinfo.io/1.1.1.1/json",
    ]


def test_location_mmdb_record_has_no_anycast_field():
    """IP-to-Geolocation MMDB: coordinates but nothing about anycast."""
    record = {
        "city": "Mountain View",
        "country": "United States",
        "country_code": "US",
        "latitude": 37.4056,
        "longitude": -122.0775,
        "region": "California",
    }

    location = location_from_record(record, "ipinfo_mmdb")

    assert (location.lat, location.lon, location.country) == (37.4056, -122.0775, "US")
    assert location.asn is None
    assert not location.anycast_known


def test_flat_mmdb_record_from_core_database():
    record = {
        "network": "66.202.64.0/19",
        "city": "Chicago",
        "country_code": "US",
        "latitude": 41.85003,
        "longitude": -87.65005,
        "asn": "AS7029",
        "is_anycast": False,
    }

    location = location_from_record(record, "ipinfo_mmdb")

    assert (location.lat, location.lon, location.asn, location.country) == (
        41.85003,
        -87.65005,
        7029,
        "US",
    )
    assert not location.is_anycast


def test_string_coordinates_and_invalid_values():
    assert location_from_record({"lat": "48.86", "lng": "2.35"}, "ipinfo_mmdb").lat == 48.86
    bad = location_from_record({"latitude": "n/a", "longitude": 999, "asn": "x"}, "ipinfo_mmdb")
    assert (bad.lat, bad.lon, bad.asn) == (None, None, None)


class FakeSource:
    def __init__(self, result=None, error=None, provides_anycast=False):
        self.result, self.error, self.calls = result, error, 0
        self.provides_anycast = provides_anycast

    async def lookup(self, address):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


async def test_geolocator_prefers_first_result_with_coordinates():
    from cadns.geo.ipinfo import GeoLookupError

    partial = FakeSource(Location("ipinfo_mmdb", country="US", asn=15169))
    failing = FakeSource(error=GeoLookupError("quota"))
    full = FakeSource(Location("ipinfo_api", lat=1.0, lon=2.0))

    assert await Geolocator([partial, failing, full]).locate("8.8.8.8") == full.result
    assert await Geolocator([partial, failing]).locate("8.8.8.8") == partial.result


@pytest.mark.parametrize("address", ["10.1.2.3", "192.0.2.1", "fd00::1", "127.0.0.1"])
async def test_geolocator_skips_non_global_addresses(address):
    source = FakeSource(Location("ipinfo_api", lat=1.0, lon=2.0))

    assert await Geolocator([source]).locate(address) is None
    assert source.calls == 0


async def test_anycast_flag_is_fetched_when_the_database_has_none():
    """The location MMDB gives coordinates; the API says whether it is anycast."""
    mmdb = FakeSource(Location("ipinfo_mmdb", lat=37.4, lon=-122.0))
    api_source = FakeSource(
        Location("ipinfo_api", lat=1.0, lon=2.0, asn=15169, is_anycast=True, anycast_known=True),
        provides_anycast=True,
    )

    location = await Geolocator([mmdb, api_source]).locate("8.8.8.8")

    assert location.source == "ipinfo_mmdb"
    assert (location.lat, location.lon) == (37.4, -122.0)  # database coordinates kept
    assert location.is_anycast
    assert location.anycast_known
    assert location.asn == 15169  # filled in from the API
    assert api_source.calls == 1


async def test_anycast_lookup_can_be_disabled():
    mmdb = FakeSource(Location("ipinfo_mmdb", lat=37.4, lon=-122.0))
    api_source = FakeSource(
        Location("ipinfo_api", is_anycast=True, anycast_known=True), provides_anycast=True
    )

    location = await Geolocator([mmdb, api_source], anycast_lookup=False).locate("8.8.8.8")

    assert not location.is_anycast
    assert api_source.calls == 0


async def test_no_extra_lookup_when_the_database_knows_about_anycast():
    core = FakeSource(Location("ipinfo_mmdb", lat=37.4, lon=-122.0, anycast_known=True))
    api_source = FakeSource(Location("ipinfo_api"), provides_anycast=True)

    await Geolocator([core, api_source]).locate("8.8.8.8")

    assert api_source.calls == 0


async def test_concurrent_lookups_all_fall_back_to_the_legacy_endpoint():
    """Each call retries on 403, even if another call already flipped the flag."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ipinfo.io":
            return httpx.Response(403, json={"error": "not available on your plan"})
        return httpx.Response(200, json=api_fixture("ipinfo_legacy_8.8.8.8"))

    async with api(handler) as client:
        source = ApiSource("t", client)
        results = await asyncio.gather(*(source.lookup(f"8.8.8.{i}") for i in range(1, 6)))

    assert all(r is not None and r.has_coordinates for r in results)
