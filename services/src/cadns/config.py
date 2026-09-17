"""Settings from environment variables (prefix CADNS_).

Database connection settings use libpq's standard PG* variables instead
(PGHOST, PGUSER, PGPASSWORD, ...), like the resolver's DLZ module.
"""

import ipaddress
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from cadns.queue import QueuePolicy

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

    # Worker and queue (Phase 4).
    worker_concurrency: int = Field(default=4, ge=1, le=64)
    worker_grace_seconds: float = Field(default=20.0, ge=0)
    queue_poll_seconds: float = Field(default=10.0, gt=0)
    queue_lock_timeout_seconds: int = Field(default=300, ge=1)
    queue_max_attempts: int = Field(default=5, ge=1)
    queue_retry_base_seconds: int = Field(default=30, ge=1)
    queue_retry_max_seconds: int = Field(default=3600, ge=1)
    queue_dead_cooldown_seconds: int = Field(default=86400, ge=0)

    # Monitor (Phase 5, ADR-10).
    monitor_scan_seconds: float = Field(default=5.0, gt=0)
    monitor_lead_seconds: int = Field(default=15, ge=0)
    monitor_activity_window_seconds: int = Field(default=3600, ge=1)
    monitor_min_remeasure_seconds: int = Field(default=10, ge=0)
    monitor_carbon_check_seconds: float = Field(default=60.0, gt=0)
    carbon_period_seconds: int = Field(default=300, ge=1)  # WattTime's data period
    carbon_unavailable_backoff_seconds: int = Field(default=3600, ge=0)
    monitor_gc_seconds: float = Field(default=3600.0, gt=0)
    gc_domain_retention_days: int = Field(default=7, ge=1)
    gc_endpoint_retention_days: int = Field(default=30, ge=1)
    gc_carbon_history_days: int = Field(default=30, ge=0)  # 0 keeps all signals

    # Collector (Phase 4).
    dnstap_socket: Path = Path("/run/cadns/dnstap.sock")
    collector_flush_seconds: float = Field(default=1.0, gt=0)
    collector_max_batch: int = Field(default=5000, ge=1)
    # Never measured: single-label names and names under these suffixes
    # (special-use names, RFC 6761 / RFC 6762 / RFC 9476, and the RFC 2606
    # documentation domains).
    collector_ignore_suffixes: str = (
        "test,example,invalid,localhost,local,onion,alt,arpa,internal,lan,home,corp,"
        "example.com,example.net,example.org"
    )

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

    @cached_property
    def ignore_suffixes(self) -> frozenset[str]:
        return frozenset(
            part.strip().strip(".").lower()
            for part in self.collector_ignore_suffixes.split(",")
            if part.strip().strip(".")
        )

    @cached_property
    def queue_policy(self) -> "QueuePolicy":
        from cadns.queue import QueuePolicy

        return QueuePolicy(
            lock_timeout_seconds=self.queue_lock_timeout_seconds,
            max_attempts=self.queue_max_attempts,
            retry_base_seconds=self.queue_retry_base_seconds,
            retry_max_seconds=self.queue_retry_max_seconds,
            dead_cooldown_seconds=self.queue_dead_cooldown_seconds,
        )
