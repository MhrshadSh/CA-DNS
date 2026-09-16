"""WattTime API v3 client (https://docs.watttime.org).

- /login (HTTP Basic) returns a bearer token valid for 30 minutes; it is
  renewed before expiry and on any 401.
- /v3/region-from-loc maps coordinates to a grid region.
- /v3/forecast?horizon_hours=0 returns the current MOER value for a region.
  MOER is reported in lbs CO2/MWh and stored in g CO2/kWh.
- Requests are rate limited client-side; HTTP 429 is retried with backoff.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.watttime.org"
TOKEN_LIFETIME_SECONDS = 25 * 60  # WattTime tokens expire after 30 minutes
MAX_RETRIES = 3

# grams CO2 per kWh, per unit of the reported value
_UNIT_FACTORS = {
    "lbs_co2_per_mwh": 453.59237 / 1000,
    "lbs_co2e_per_mwh": 453.59237 / 1000,
    "g_co2_per_kwh": 1.0,
    "g_co2e_per_kwh": 1.0,
    "kg_co2_per_mwh": 1.0,
    "kg_co2e_per_mwh": 1.0,
}


class WattTimeError(Exception):
    pass


@dataclass(frozen=True)
class Region:
    code: str
    name: str | None


@dataclass(frozen=True)
class Signal:
    region: str
    point_time: datetime
    moer_g_per_kwh: float


def to_g_per_kwh(value: float, units: str) -> float:
    try:
        return value * _UNIT_FACTORS[units.lower()]
    except KeyError:
        raise WattTimeError(f"unsupported MOER units: {units!r}") from None


class RateLimiter:
    """Minimum spacing between requests (a token bucket of size 1)."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class WattTimeClient:
    def __init__(
        self,
        username: str,
        password: str,
        client: httpx.AsyncClient,
        *,
        signal_type: str = "co2_moer",
        requests_per_second: float = 10.0,
    ) -> None:
        self._auth = httpx.BasicAuth(username, password)
        self._client = client
        self.signal_type = signal_type
        self._limiter = RateLimiter(requests_per_second)
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock = asyncio.Lock()

    async def region_for(self, lat: float, lon: float) -> Region | None:
        """Grid region serving a location; None if outside WattTime coverage."""
        response = await self._get(
            "/v3/region-from-loc",
            {"latitude": lat, "longitude": lon, "signal_type": self.signal_type},
        )
        if response.status_code in (400, 404):
            return None
        self._raise_for_status(response, "region-from-loc")
        body = response.json()
        return Region(code=body["region"], name=body.get("region_full_name"))

    async def current_moer(self, region: str) -> Signal | None:
        """Most recent MOER for a region; None if the account has no access."""
        response = await self._get(
            "/v3/forecast",
            {"region": region, "signal_type": self.signal_type, "horizon_hours": 0},
        )
        if response.status_code == 403:
            log.warning("no WattTime %s access for region %s", self.signal_type, region)
            return None
        self._raise_for_status(response, "forecast")
        body = response.json()
        if not body.get("data"):
            return None
        point = body["data"][0]
        return Signal(
            region=region,
            point_time=datetime.fromisoformat(point["point_time"]),
            moer_g_per_kwh=to_g_per_kwh(float(point["value"]), body["meta"]["units"]),
        )

    async def _login(self) -> str:
        async with self._login_lock:
            if self._token is not None and time.monotonic() < self._token_expires:
                return self._token
            await self._limiter.wait()
            try:
                response = await self._client.get(f"{API_BASE}/login", auth=self._auth)
            except httpx.HTTPError as exc:
                raise WattTimeError(f"login failed: {exc}") from exc
            if response.status_code != 200:
                raise WattTimeError(f"login failed: HTTP {response.status_code}")
            self._token = response.json()["token"]
            self._token_expires = time.monotonic() + TOKEN_LIFETIME_SECONDS
            return self._token

    async def _get(self, path: str, params: dict[str, object]) -> httpx.Response:
        relogged = False
        for attempt in range(MAX_RETRIES + 1):
            token = await self._login()
            await self._limiter.wait()
            try:
                response = await self._client.get(
                    f"{API_BASE}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise WattTimeError(f"{path} request failed: {exc}") from exc

            if response.status_code == 401 and not relogged:
                self._token = None  # expired early: log in again once
                relogged = True
                continue
            if response.status_code == 429 and attempt < MAX_RETRIES:
                delay = _retry_after(response, default=2.0**attempt)
                log.warning("WattTime rate limit hit; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            return response
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, what: str) -> None:
        if response.status_code != 200:
            raise WattTimeError(f"{what}: HTTP {response.status_code}: {response.text[:200]}")


def _retry_after(response: httpx.Response, default: float) -> float:
    try:
        return min(60.0, max(0.0, float(response.headers["Retry-After"])))
    except (KeyError, ValueError):
        return default
