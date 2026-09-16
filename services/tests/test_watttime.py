import base64
from datetime import UTC, datetime

import httpx
import pytest
from cadns.carbon import WattTimeClient, WattTimeError, to_g_per_kwh
from conftest import api_fixture


class FakeWattTime:
    """Routes requests like api.watttime.org and records them."""

    def __init__(self, **overrides):
        self.requests: list[httpx.Request] = []
        self.overrides = overrides
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        override = self.overrides.get(path)
        if callable(override):
            response = override(request)
            if response is not None:
                return response
        if path == "/login":
            self.logins += 1
            return httpx.Response(200, json={"token": f"token-{self.logins}"})
        if path == "/v3/region-from-loc":
            return httpx.Response(200, json=api_fixture("watttime_region_from_loc"))
        if path == "/v3/forecast":
            return httpx.Response(200, json=api_fixture("watttime_forecast_h0"))
        return httpx.Response(404)


def client_for(fake: FakeWattTime) -> tuple[httpx.AsyncClient, WattTimeClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return http, WattTimeClient("alice", "s3cret", http, requests_per_second=1000)


async def test_login_once_and_bearer_token_on_data_calls():
    fake = FakeWattTime()
    http, wt = client_for(fake)
    async with http:
        region = await wt.region_for(38.58, -121.49)
        signal = await wt.current_moer("CAISO_NORTH")

    assert region.code == "CAISO_NORTH"
    assert region.name == "California ISO Northern"
    assert fake.logins == 1
    login, *data = fake.requests
    assert login.headers["Authorization"] == "Basic " + base64.b64encode(b"alice:s3cret").decode()
    assert all(r.headers["Authorization"] == "Bearer token-1" for r in data)
    assert dict(data[0].url.params) == {
        "latitude": "38.58",
        "longitude": "-121.49",
        "signal_type": "co2_moer",
    }
    assert dict(data[1].url.params) == {
        "region": "CAISO_NORTH",
        "signal_type": "co2_moer",
        "horizon_hours": "0",
    }
    assert signal.point_time == datetime(2026, 9, 15, 12, 5, tzinfo=UTC)
    assert signal.moer_g_per_kwh == pytest.approx(850.0 * 0.45359237)


async def test_expired_token_triggers_one_new_login():
    rejected = []

    def forecast(request):
        if request.headers["Authorization"] == "Bearer token-1":
            rejected.append(request)
            return httpx.Response(401)
        return None

    fake = FakeWattTime(**{"/v3/forecast": forecast})
    http, wt = client_for(fake)
    async with http:
        signal = await wt.current_moer("CAISO_NORTH")

    assert signal is not None
    assert fake.logins == 2
    assert len(rejected) == 1


async def test_location_outside_coverage_is_none():
    fake = FakeWattTime(**{"/v3/region-from-loc": lambda r: httpx.Response(404, json={})})
    http, wt = client_for(fake)
    async with http:
        assert await wt.region_for(0.0, 0.0) is None


async def test_region_without_access_is_none():
    fake = FakeWattTime(**{"/v3/forecast": lambda r: httpx.Response(403, json={"error": "x"})})
    http, wt = client_for(fake)
    async with http:
        assert await wt.current_moer("DE") is None


async def test_rate_limit_response_is_retried():
    attempts = []

    def forecast(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return None

    fake = FakeWattTime(**{"/v3/forecast": forecast})
    http, wt = client_for(fake)
    async with http:
        assert await wt.current_moer("CAISO_NORTH") is not None
    assert len(attempts) == 2


async def test_login_failure_raises():
    fake = FakeWattTime(**{"/login": lambda r: httpx.Response(403)})
    http, wt = client_for(fake)
    async with http:
        with pytest.raises(WattTimeError, match="login failed"):
            await wt.current_moer("CAISO_NORTH")


@pytest.mark.parametrize(
    ("value", "units", "expected"),
    [
        (1000, "lbs_co2_per_mwh", 453.59237),
        (400, "g_co2_per_kwh", 400),
        (400, "kg_co2_per_mwh", 400),
    ],
)
def test_unit_conversion(value, units, expected):
    assert to_g_per_kwh(value, units) == pytest.approx(expected)


def test_unknown_units_raise():
    with pytest.raises(WattTimeError):
        to_g_per_kwh(1, "percentile")
