import json
import os
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import dns.message
import psycopg
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


def dnstap_frames() -> list[bytes]:
    """Client responses recorded from BIND 9.20 (length-prefixed frames), in order:
    www.example.test A (aa), www.example.test AAAA (aa), example.com A (recursive),
    cadns-nonexistent-fixture-name.com A (NXDOMAIN), example.com TXT, www.example.test TXT (aa).
    """
    data = (FIXTURES / "dnstap" / "bind-9.20-client-responses.frames").read_bytes()
    frames, pos = [], 0
    while pos < len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        frames.append(data[pos + 4 : pos + 4 + length])
        pos += 4 + length
    return frames


def require_database() -> None:
    if "PGHOST" not in os.environ:
        pytest.skip("no database configured (run with make test)")


@pytest.fixture
async def committed() -> AsyncIterator[psycopg.AsyncConnection]:
    """Autocommit connection to the test database; queue and domains are emptied
    before and after. Refuses to run anywhere but the dedicated test database."""
    require_database()
    conn = await psycopg.AsyncConnection.connect(autocommit=True)
    async with conn:
        (database,) = await (await conn.execute("SELECT current_database()")).fetchone()
        if database != os.environ.get("CADNS_TEST_DATABASE_NAME", "cadns_test"):
            pytest.fail(f"refusing to empty tables in database {database!r}")

        async def empty() -> None:
            await conn.execute("DELETE FROM cadns.measurement_queue")
            await conn.execute("DELETE FROM cadns.domains")

        await empty()
        try:
            yield conn
        finally:
            await empty()


@pytest.fixture
async def tx() -> AsyncIterator[psycopg.AsyncConnection]:
    """Connection to the test database inside a transaction that is rolled back."""
    require_database()
    conn = await psycopg.AsyncConnection.connect()
    async with conn, conn.transaction(force_rollback=True):
        yield conn
