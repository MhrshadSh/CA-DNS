"""BIND + dlz_pgsql end to end: authoritative green answers, recursion, fail-open.

Tests marked `internet` need the resolver to reach the root servers.
"""

from concurrent.futures import ThreadPoolExecutor

import dns.flags
import dns.rcode
import dns.rdatatype
import pytest
from resolver.conftest import SUFFIX

NAME = f"www.{SUFFIX}"


def is_authoritative(response) -> bool:
    return bool(response.flags & dns.flags.AA)


def serve(served, name=NAME) -> None:
    """A name with a green and a dirty endpoint for A and AAAA."""
    served.region("T_RES_LOW", 40)
    served.region("T_RES_HIGH", 700)
    served.endpoint("10.99.0.1", "T_RES_HIGH")
    served.endpoint("10.99.0.2", "T_RES_LOW")
    served.endpoint("fd00:99::1", "T_RES_HIGH")
    served.endpoint("fd00:99::2", "T_RES_LOW")
    for address in ("10.99.0.1", "10.99.0.2", "fd00:99::1", "fd00:99::2"):
        served.record(name, address)


def test_measured_name_gets_greenest_authoritative_answer(served, resolver):
    serve(served)

    a = resolver.query(NAME, "A")
    aaaa = resolver.query(NAME, "AAAA")

    assert a.rcode() == dns.rcode.NOERROR
    assert is_authoritative(a)
    assert resolver.addresses(a) == ["10.99.0.2"]
    assert a.answer[0].ttl <= 300
    assert is_authoritative(aaaa)
    assert resolver.addresses(aaaa) == ["fd00:99::2"]


def test_qname_case_does_not_matter(served, resolver):
    serve(served)

    response = resolver.query(NAME.upper())

    assert is_authoritative(response)
    assert resolver.addresses(response) == ["10.99.0.2"]


def test_answer_follows_database_changes_immediately(served, resolver):
    serve(served)
    assert resolver.addresses(resolver.query(NAME)) == ["10.99.0.2"]

    served.region("T_RES_HIGH", 5)  # newer, greener signal for the other region

    assert resolver.addresses(resolver.query(NAME)) == ["10.99.0.1"]


def test_other_qtype_on_served_name_is_authoritative_nodata(served, resolver):
    """Known v1 limitation (architecture §5.1 #5): TXT/HTTPS/MX get NODATA."""
    serve(served)

    response = resolver.query(NAME, "TXT")

    assert response.rcode() == dns.rcode.NOERROR
    assert is_authoritative(response)
    assert response.answer == []
    assert [rrset.rdtype for rrset in response.authority] == [dns.rdatatype.SOA]


@pytest.mark.internet
def test_unmeasured_name_is_resolved_recursively(resolver):
    response = resolver.query("example.com")

    assert response.rcode() == dns.rcode.NOERROR
    assert not is_authoritative(response)
    assert resolver.addresses(response)


@pytest.mark.internet
@pytest.mark.parametrize("child", [f"x.{NAME}", f"a.b.{NAME}"])
def test_children_of_served_name_are_not_served(served, resolver, child):
    """ADR-8: only exact names; children recurse (NXDOMAIN from the root for .test)."""
    serve(served)

    response = resolver.query(child)

    assert not is_authoritative(response)
    assert response.rcode() == dns.rcode.NXDOMAIN


@pytest.mark.internet
def test_expired_name_is_resolved_recursively(served, resolver):
    served.record(NAME, "10.99.0.1", ttl=60, age=120)

    response = resolver.query(NAME)

    assert not is_authoritative(response)


@pytest.mark.internet
def test_parent_and_child_probes_under_concurrency(served, resolver):
    """Interleaved parent/child queries must never serve the parent's data to a child."""
    serve(served)
    names = [NAME, f"x.{NAME}", NAME, f"deep.x.{NAME}"] * 100

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda n: (n, resolver.query(n)), names))

    served_answers = [is_authoritative(r) for n, r in results if n == NAME]
    child_answers = [is_authoritative(r) for n, r in results if n != NAME]
    assert all(served_answers), f"{served_answers.count(False)} parent queries not served"
    assert not any(child_answers), f"{sum(child_answers)} child queries served"


@pytest.mark.internet
def test_database_error_fails_open(served, resolver, owner_conn):
    """A failing lookup makes BIND answer by recursion instead of SERVFAIL."""
    serve(served)
    assert is_authoritative(resolver.query(NAME))

    with owner_conn.cursor() as cur:
        cur.execute("REVOKE EXECUTE ON FUNCTION cadns.dlz_lookup(text, text) FROM cadns_dlz")
        try:
            response = resolver.query(NAME)
            assert not is_authoritative(response)
            assert response.rcode() == dns.rcode.NXDOMAIN  # recursion: .test does not exist
            assert resolver.addresses(resolver.query("example.com"))
        finally:
            cur.execute("GRANT EXECUTE ON FUNCTION cadns.dlz_lookup(text, text) TO cadns_dlz")

    assert is_authoritative(resolver.query(NAME))
