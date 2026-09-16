import dns.exception
import dns.message
import dns.rcode
import dns.rdatatype
from cadns.resolvers import Answer, ResolverPool, parse_response
from conftest import dns_fixture


def test_follows_cname_chain_and_uses_minimum_ttl():
    answer = parse_response(dns_fixture("cname_chain_a"), "www.bing.com", "A", "8.8.8.8")

    assert answer.status == "noerror"
    assert answer.addresses == ("2.16.106.200", "2.16.106.207", "2.16.106.215")
    assert answer.ttl == 20  # CNAMEs have 1656/60/1657, the A RRset 20


def test_follows_cname_chain_for_aaaa():
    answer = parse_response(dns_fixture("cname_chain_aaaa"), "www.microsoft.com", "AAAA", "1.1.1.1")

    assert answer.addresses == ("2a02:26f0:1180:183::356e", "2a02:26f0:1180:19b::356e")
    assert answer.ttl == 20
    assert answer.resolver == "1.1.1.1"


def test_direct_address_rrset():
    answer = parse_response(dns_fixture("youtube_a"), "www.youtube.com", "A", "8.8.8.8")

    assert answer.status == "noerror"
    assert answer.addresses
    assert answer.ttl == 300


def test_qname_case_is_ignored():
    answer = parse_response(dns_fixture("youtube_a"), "WWW.YouTube.com", "A", "8.8.8.8")

    assert answer.addresses


def test_nodata_carries_negative_ttl():
    answer = parse_response(dns_fixture("nodata_aaaa"), "ipv4only.arpa", "AAAA", "8.8.8.8")

    assert answer.status == "noerror"
    assert answer.addresses == ()
    assert answer.ttl is None
    assert answer.negative_ttl == 266  # min(SOA TTL 266, SOA minimum 3600)
    assert answer.definitive


def test_nxdomain_carries_negative_ttl():
    answer = parse_response(
        dns_fixture("nxdomain_a"), "does-not-exist.cadns-fixture.invalid", "A", "8.8.8.8"
    )

    assert answer.status == "nxdomain"
    assert answer.negative_ttl == 86399
    assert answer.definitive


def test_wrong_type_in_answer_is_not_used():
    answer = parse_response(dns_fixture("youtube_a"), "www.youtube.com", "AAAA", "8.8.8.8")

    assert answer.addresses == ()


def test_servfail_is_not_definitive():
    response = dns.message.make_response(dns.message.make_query("example.com", "A"))
    response.set_rcode(dns.rcode.SERVFAIL)

    answer = parse_response(response, "example.com", "A", "9.9.9.9")

    assert answer.status == "servfail"
    assert not answer.definitive


def test_cname_loop_ends_without_addresses():
    query = dns.message.make_query("a.example.", "A")
    response = dns.message.from_text(
        f"""id {query.id}
opcode QUERY
rcode NOERROR
flags QR RD RA
;QUESTION
a.example. IN A
;ANSWER
a.example. 60 IN CNAME b.example.
b.example. 60 IN CNAME a.example.
"""
    )

    answer = parse_response(response, "a.example", "A", "8.8.8.8")

    assert answer.status == "noerror"
    assert answer.addresses == ()


async def test_pool_queries_every_resolver_for_both_types():
    calls = []

    async def fake_query(request, where, timeout):
        rtype = dns.rdatatype.to_text(request.question[0].rdtype)
        calls.append((where, rtype, timeout))
        if where == "9.9.9.9":
            raise dns.exception.Timeout
        if where == "1.1.1.1":
            raise OSError("network unreachable")
        return dns_fixture("youtube_a" if rtype == "A" else "youtube_aaaa")

    pool = ResolverPool(["8.8.8.8", "9.9.9.9", "1.1.1.1"], timeout=1.5, query=fake_query)
    answers = await pool.resolve("www.youtube.com")

    assert sorted((w, t) for w, t, _ in calls) == sorted(
        (w, t) for w in ("8.8.8.8", "9.9.9.9", "1.1.1.1") for t in ("A", "AAAA")
    )
    assert {c[2] for c in calls} == {1.5}
    by_key = {(a.resolver, a.rtype): a for a in answers}
    assert by_key[("9.9.9.9", "A")] == Answer("9.9.9.9", "A", "timeout")
    assert by_key[("1.1.1.1", "AAAA")].status == "error"
    aaaa = by_key[("8.8.8.8", "AAAA")]
    assert aaaa.status == "noerror"
    assert len(aaaa.addresses) == 8
    assert "2001:4860:4827:400::" in aaaa.addresses
