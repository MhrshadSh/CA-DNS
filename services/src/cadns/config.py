"""Settings from environment variables (prefix CADNS_).

Database connection settings use libpq's standard PG* variables instead
(PGHOST, PGUSER, PGPASSWORD, ...), like the resolver's DLZ module.
"""

import ipaddress
from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_RESOLVERS = "8.8.8.8,1.1.1.1,9.9.9.9,45.90.28.243,208.67.222.222"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CADNS_", frozen=True)

    # Upstream resolver pool (comma-separated IPs) and per-query timeout.
    upstream_resolvers: str = DEFAULT_RESOLVERS
    resolver_timeout: float = Field(default=2.0, gt=0, le=30)

    # IPinfo: MMDB (used if the file exists) with API fallback (if a token is set).
    ipinfo_mmdb: Path | None = Path("/data/ipinfo/ipinfo_location.mmdb")
    ipinfo_token: SecretStr | None = None
    # The IP-to-Geolocation MMDB has no anycast field; look it up through the
    # API for endpoints that are new or stale (ADR-5 ranks anycast as unknown).
    anycast_lookup: bool = True
    geo_max_age_days: int = Field(default=30, ge=1)

    # WattTime.
    watttime_username: str | None = None
    watttime_password: SecretStr | None = None
    watttime_signal_type: str = "co2_moer"
    watttime_requests_per_second: float = Field(default=10.0, gt=0)
    carbon_max_age_seconds: int = Field(default=300, ge=1)
    # Locations outside WattTime coverage are looked up again after this.
    region_negative_days: int = Field(default=30, ge=1)

    # Backoff for negative and failed measurements (domains.retry_after).
    negative_min_seconds: int = Field(default=300, ge=0)
    negative_max_seconds: int = Field(default=3600, ge=0)
    failed_retry_seconds: int = Field(default=60, ge=0)

    http_timeout: float = Field(default=10.0, gt=0)

    @field_validator("ipinfo_token", "watttime_password", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("watttime_username", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("upstream_resolvers")
    @classmethod
    def _valid_resolvers(cls, value: str) -> str:
        addresses = [part.strip() for part in value.split(",") if part.strip()]
        if not addresses:
            raise ValueError("at least one upstream resolver is required")
        for address in addresses:
            ipaddress.ip_address(address)
        return ",".join(addresses)

    @cached_property
    def resolvers(self) -> tuple[str, ...]:
        return tuple(self.upstream_resolvers.split(","))
