"""Answer policy: cadns.dlz_findzone and cadns.dlz_lookup (architecture ADR-5)."""

from collections import Counter

import pytest

ZONE = "www.policy.test"


def test_answers_lowest_moer_address(db):
    db.region("T_HIGH", 800)
    db.region("T_MID", 400)
    db.region("T_LOW", 50)
    db.endpoint("10.0.0.1", "T_HIGH")
    db.endpoint("10.0.0.2", "T_LOW")
    db.endpoint("10.0.0.3", "T_MID")
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        db.record(ZONE, address)

    assert db.answers(ZONE) == ["10.0.0.2"]


def test_answer_k_orders_by_moer_with_unknown_last(db):
    db.settings(answer_k=10)
    db.region("T_HIGH", 800)
    db.region("T_LOW", 50)
    db.cur.execute("INSERT INTO cadns.grid_regions (code) VALUES ('T_NO_SIGNAL')")
    db.endpoint("10.0.0.1", "T_HIGH")
    db.endpoint("10.0.0.2", "T_LOW")
    db.endpoint("10.0.0.3", "T_NO_SIGNAL")  # region without a carbon signal
    db.endpoint("10.0.0.4", "T_LOW", anycast=True)  # anycast: location unreliable
    db.endpoint("10.0.0.5")  # not geolocated
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"):
        db.record(ZONE, address)

    answers = db.answers(ZONE)
    assert answers[:2] == ["10.0.0.2", "10.0.0.1"]
    assert sorted(answers[2:]) == ["10.0.0.3", "10.0.0.4", "10.0.0.5"]


def test_answer_k_limits_each_rrset_type(db):
    db.settings(answer_k=2)
    for i in range(1, 5):
        db.record(ZONE, f"10.0.0.{i}")
        db.record(ZONE, f"fd00::{i}")

    assert len(db.answers(ZONE, "A")) == 2
    assert len(db.answers(ZONE, "AAAA")) == 2


def test_uses_latest_carbon_signal(db):
    db.region("T_WAS_GREEN", 10, age=600)
    db.region("T_WAS_GREEN", 900, age=0)
    db.region("T_STEADY", 300)
    db.endpoint("10.0.0.1", "T_WAS_GREEN")
    db.endpoint("10.0.0.2", "T_STEADY")
    db.record(ZONE, "10.0.0.1")
    db.record(ZONE, "10.0.0.2")

    assert db.answers(ZONE) == ["10.0.0.2"]


def test_ties_are_broken_randomly(db):
    db.region("T_SAME", 100)
    db.endpoint("10.0.0.1", "T_SAME")
    db.endpoint("10.0.0.2", "T_SAME")
    db.record(ZONE, "10.0.0.1")
    db.record(ZONE, "10.0.0.2")

    picks = Counter(db.answers(ZONE)[0] for _ in range(100))

    # Both addresses must show up; a fixed order fails with probability ~2^-99.
    assert set(picks) == {"10.0.0.1", "10.0.0.2"}


def test_all_unknown_moer_still_answers(db):
    db.record(ZONE, "10.0.0.1")
    db.record(ZONE, "10.0.0.2")

    assert db.answers(ZONE)[0] in {"10.0.0.1", "10.0.0.2"}


def test_same_address_from_several_resolvers_is_answered_once(db):
    db.settings(answer_k=10)
    db.record(ZONE, "10.0.0.1", resolver="8.8.8.8", ttl=100)
    db.record(ZONE, "10.0.0.1", resolver="1.1.1.1", ttl=200)

    assert db.answers(ZONE) == ["10.0.0.1"]
    # The address stays valid as long as any resolver's record does.
    assert db.ttls(ZONE)["A"] == 200


def test_expired_records_are_not_candidates(db):
    db.region("T_LOW", 50)
    db.region("T_HIGH", 800)
    db.endpoint("10.0.0.1", "T_LOW")
    db.endpoint("10.0.0.2", "T_HIGH")
    db.record(ZONE, "10.0.0.1", ttl=60, age=120)  # greenest, but expired
    db.record(ZONE, "10.0.0.2", ttl=3600)

    assert db.answers(ZONE) == ["10.0.0.2"]


def test_findzone_true_when_every_rrset_type_is_fresh(db):
    db.record(ZONE, "10.0.0.1")
    db.record(ZONE, "fd00::1")

    assert db.findzone(ZONE)


def test_findzone_false_when_all_records_expired(db):
    db.record(ZONE, "10.0.0.1", ttl=60, age=61)

    assert not db.findzone(ZONE)
    assert db.lookup(ZONE) == []


def test_findzone_false_when_one_rrset_type_expired(db):
    """Serving A while AAAA is stale would answer AAAA queries with NODATA."""
    db.record(ZONE, "10.0.0.1", ttl=3600)
    db.record(ZONE, "fd00::1", ttl=60, age=61)

    assert not db.findzone(ZONE)
    assert db.lookup(ZONE) == []


def test_findzone_false_for_expiry_boundary(db):
    db.record(ZONE, "10.0.0.1", ttl=60, age=60)

    assert not db.findzone(ZONE)


@pytest.mark.parametrize("name", ["unknown.policy.test", "policy.test", "test", ""])
def test_findzone_false_for_names_without_data(db, name):
    db.record(ZONE, "10.0.0.1")

    assert not db.findzone(name)


def test_domain_without_records_is_not_served(db):
    db.cur.execute("INSERT INTO cadns.domains (name, status) VALUES (%s, 'nxdomain')", (ZONE,))

    assert not db.findzone(ZONE)


@pytest.mark.parametrize("variant", ["WWW.Policy.TEST", "www.policy.test.", "Www.Policy.Test."])
def test_names_are_case_and_trailing_dot_insensitive(db, variant):
    db.record(ZONE, "10.0.0.1")

    assert db.findzone(variant)
    assert db.answers(variant) == ["10.0.0.1"]


@pytest.mark.parametrize("name", ["@", "", ZONE, ZONE.upper() + "."])
def test_apex_name_forms(db, name):
    db.record(ZONE, "10.0.0.1")

    assert [row[1] for row in db.lookup(ZONE, name)] == ["SOA", "NS", "A"]


@pytest.mark.parametrize("name", ["x", "sub.www", "x.www.policy.test"])
def test_child_names_have_no_records(db, name):
    db.record(ZONE, "10.0.0.1")

    assert db.lookup(ZONE, name) == []


@pytest.mark.parametrize(
    ("ttl", "age", "max_answer_ttl", "expected"),
    [
        (100, 0, 300, 100),  # remaining TTL below the cap
        (3600, 0, 300, 300),  # capped at the carbon refresh interval
        (3600, 3500, 300, 100),  # remaining, not original, TTL
        (60, 59.6, 300, 1),  # sub-second remainder never becomes 0
        (3600, 0, 60, 60),  # cap is configurable
    ],
)
def test_ttl_is_remaining_ttl_clamped(db, ttl, age, max_answer_ttl, expected):
    db.settings(max_answer_ttl=max_answer_ttl)
    db.record(ZONE, "10.0.0.1", ttl=ttl, age=age)

    assert db.ttls(ZONE)["A"] == expected


def test_rrset_ttl_is_minimum_of_returned_records(db):
    db.settings(answer_k=2)
    db.record(ZONE, "10.0.0.1", ttl=100)
    db.record(ZONE, "10.0.0.2", ttl=200)
    db.record(ZONE, "fd00::1", ttl=250)

    rows = db.lookup(ZONE)
    assert {ttl for ttl, type_, _ in rows if type_ == "A"} == {100}
    assert db.ttls(ZONE)["AAAA"] == 250


def test_soa_and_ns_are_synthesised_at_apex(db):
    db.settings(ns_name="ns.example.", hostmaster="admin.example.")
    db.record(ZONE, "10.0.0.1", ttl=100, age=10)
    db.record(ZONE, "fd00::1", ttl=200)
    db.cur.execute("SELECT floor(extract(epoch FROM now()))::bigint")
    serial = db.cur.fetchone()[0]

    rows = db.lookup(ZONE)

    assert rows[0] == (90, "SOA", f"ns.example. admin.example. {serial} 3600 600 86400 90")
    assert rows[1] == (90, "NS", "ns.example.")
    assert [row[1] for row in rows[2:]] == ["A", "AAAA"]
