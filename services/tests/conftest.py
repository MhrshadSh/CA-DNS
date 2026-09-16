import json
from pathlib import Path

import dns.message
import pytest
from cadns.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def dns_fixture(name: str) -> dns.message.Message:
    """Wire-format responses recorded from 8.8.8.8 (see tests/fixtures/dns)."""
    return dns.message.from_wire((FIXTURES / "dns" / f"{name}.bin").read_bytes())


def api_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "api" / f"{name}.json").read_text())


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for var in ("CADNS_IPINFO_TOKEN", "CADNS_WATTTIME_USERNAME", "CADNS_WATTTIME_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return Settings(ipinfo_mmdb=None)
