"""An active domain stays served (ROADMAP Phase 5 exit criterion).

Once measured, a domain that keeps being queried must keep getting
authoritative answers across several upstream TTLs: the monitor re-measures it
before its records expire. Takes a few minutes (marker `slow`).
"""

import time

import dns.flags
import pytest

DOMAIN = "www.un.org"  # a paper domain; CloudFront, 60 s TTL
QUERY_INTERVAL = 2
OBSERVE_SECONDS = 180  # three TTLs

pytestmark = [pytest.mark.internet, pytest.mark.slow]


def authoritative(response) -> bool:
    return bool(response.flags & dns.flags.AA)


def test_active_domain_is_served_across_several_ttls(resolver):
    deadline = time.monotonic() + 60
    while not authoritative(resolver.query(DOMAIN)):  # the miss triggers a measurement
        assert time.monotonic() < deadline, f"{DOMAIN} was never measured"
        time.sleep(1)

    misses, queries, ttls = [], 0, set()
    end = time.monotonic() + OBSERVE_SECONDS
    while time.monotonic() < end:
        response = resolver.query(DOMAIN)
        queries += 1
        if authoritative(response):
            ttls.add(response.answer[0].ttl)
        else:
            misses.append(round(OBSERVE_SECONDS - (end - time.monotonic())))
        time.sleep(QUERY_INTERVAL)

    assert queries > OBSERVE_SECONDS // (QUERY_INTERVAL + 1)
    assert misses == [], f"not served at {misses}s of {OBSERVE_SECONDS}s ({queries} queries)"
