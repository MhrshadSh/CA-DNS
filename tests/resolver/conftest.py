"""Fixtures for tests that query the resolver (BIND + dlz_pgsql) over DNS.

The resolver reads through its own database connections, so data must be
committed. Everything lives under SUFFIX and TEST_NET and is removed before
and after each test.
"""

import socket
from collections.abc import Iterator

import dns.message
import dns.query
import dns.rdatatype
import psycopg
import pytest
from scenario import Scenario

SUFFIX = "resolver.cadns.test"
TEST_NET = "10.99.0.0/16"


def cleanup(cur: psycopg.Cursor) -> None:
    cur.execute("DELETE FROM cadns.domains WHERE name LIKE %s", (f"%.{SUFFIX}",))
    cur.execute("DELETE FROM cadns.endpoints WHERE address << %s", (TEST_NET,))
    cur.execute("DELETE FROM cadns.grid_regions WHERE code LIKE 'T_RES_%'")


@pytest.fixture
def served(owner_conn: psycopg.Connection) -> Iterator[Scenario]:
    """Scenario whose statements are committed immediately."""
    owner_conn.rollback()
    owner_conn.autocommit = True
    with owner_conn.cursor() as cursor:
        cleanup(cursor)
        try:
            yield Scenario(cursor)
        finally:
            cleanup(cursor)
            owner_conn.autocommit = False


class Resolver:
    def __init__(self, address: str) -> None:
        self.address = address

    def query(self, name: str, rtype: str = "A", timeout: float = 8) -> dns.message.Message:
        request = dns.message.make_query(name, rtype, use_edns=0)
        try:
            return dns.query.udp(request, self.address, timeout=timeout)
        except dns.exception.Timeout:
            # Cold-cache recursion can exceed one UDP attempt; retry once.
            return dns.query.udp(request, self.address, timeout=timeout)

    def addresses(self, response: dns.message.Message) -> list[str]:
        return [
            rdata.address
            for rrset in response.answer
            if rrset.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA)
            for rdata in rrset
        ]


@pytest.fixture(scope="session")
def resolver() -> Resolver:
    return Resolver(socket.gethostbyname("resolver"))
