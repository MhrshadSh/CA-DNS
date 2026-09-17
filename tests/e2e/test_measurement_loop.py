"""The whole loop on the running stack (ROADMAP Phase 4 exit criterion).

client query (miss, recursive) -> dnstap -> collector -> queue -> worker
-> measurement stored -> client query (hit, authoritative, greenest)

Needs the Internet: recursion, the upstream resolver pool and, if configured,
IPinfo and WattTime.
"""

import time

import dns.flags
import dns.rcode
import pytest

DOMAIN = "www.wikipedia.org"
TIMEOUT_SECONDS = 60

pytestmark = pytest.mark.internet


def authoritative(response) -> bool:
    return bool(response.flags & dns.flags.AA)


def execute(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def query_all(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_first_query_is_recursive_and_later_queries_get_the_greenest_endpoint(owner_conn, resolver):
    execute(owner_conn, "DELETE FROM cadns.measurement_queue WHERE domain = %s", DOMAIN)
    execute(owner_conn, "DELETE FROM cadns.domains WHERE name = %s", DOMAIN)

    first = resolver.query(DOMAIN)
    assert first.rcode() == dns.rcode.NOERROR
    assert not authoritative(first)
    assert resolver.addresses(first)

    # Clients keep asking; each miss reaches the collector (dnstap is lossy).
    response, deadline = first, time.monotonic() + TIMEOUT_SECONDS
    while not authoritative(response) and time.monotonic() < deadline:
        time.sleep(1)
        response = resolver.query(DOMAIN)
    assert authoritative(response), f"{DOMAIN} not measured within {TIMEOUT_SECONDS}s"

    (answered,) = resolver.addresses(response)
    candidates = dict(
        query_all(
            owner_conn,
            """SELECT DISTINCT host(r.address), s.moer_g_per_kwh
               FROM cadns.rrset_records r
               JOIN cadns.domains d ON d.id = r.domain_id
               JOIN cadns.endpoints e ON e.address = r.address
               LEFT JOIN LATERAL (
                   SELECT moer_g_per_kwh FROM cadns.carbon_signals
                   WHERE region_code = e.region_code AND NOT e.is_anycast
                   ORDER BY point_time DESC LIMIT 1
               ) s ON true
               WHERE d.name = %s AND r.rtype = 'A' AND r.expires_at > now()""",
            DOMAIN,
        )
    )
    assert answered in candidates
    known = [moer for moer in candidates.values() if moer is not None]
    assert candidates[answered] == (min(known) if known else None)

    # Hits are counted too (collector flushes about once a second).
    def activity():
        return query_all(
            owner_conn,
            "SELECT status, hit_count, last_queried_at IS NOT NULL "
            "FROM cadns.domains WHERE name = %s",
            DOMAIN,
        )

    deadline = time.monotonic() + 10
    while activity()[0][1] == 0 and time.monotonic() < deadline:
        time.sleep(0.5)
    status, hits, active = activity()[0]
    assert (status, active) == ("resolved", True)
    assert hits >= 1
