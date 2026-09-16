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
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

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
