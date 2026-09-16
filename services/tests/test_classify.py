import pytest
from cadns.resolvers import Answer
from cadns.worker.pipeline import classify, normalize_domain


def ok(resolver, rtype, *addresses, ttl=300):
    return Answer(resolver, rtype, "noerror", tuple(addresses), ttl if addresses else None)


def test_resolved_merges_all_resolvers(settings):
    answers = [
        ok("8.8.8.8", "A", "192.0.2.1", ttl=60),
        ok("1.1.1.1", "A", "192.0.2.1", "192.0.2.2", ttl=120),
        ok("8.8.8.8", "AAAA", "2001:db8::1"),
        Answer("1.1.1.1", "AAAA", "timeout"),  # one resolver failing is fine
    ]

    outcome = classify(answers, settings)

    assert outcome.status == "resolved"
    assert outcome.retry_after_seconds is None
    assert outcome.addresses == {"192.0.2.1", "192.0.2.2", "2001:db8::1"}
    assert {(r.address, r.resolver, r.ttl) for r in outcome.records if r.rtype == "A"} == {
        ("192.0.2.1", "8.8.8.8", 60),
        ("192.0.2.1", "1.1.1.1", 120),
        ("192.0.2.2", "1.1.1.1", 120),
    }


def test_nodata_for_one_type_is_still_resolved(settings):
    answers = [ok("8.8.8.8", "A", "192.0.2.1"), ok("8.8.8.8", "AAAA")]

    assert classify(answers, settings).status == "resolved"


def test_type_without_any_definitive_answer_fails(settings):
    """Storing A alone would make AAAA queries an authoritative NODATA."""
    answers = [
        ok("8.8.8.8", "A", "192.0.2.1"),
        Answer("8.8.8.8", "AAAA", "timeout"),
        Answer("1.1.1.1", "AAAA", "servfail"),
    ]

    outcome = classify(answers, settings)

    assert outcome.status == "failed"
    assert outcome.retry_after_seconds == settings.failed_retry_seconds


@pytest.mark.parametrize(
    ("negative_ttls", "expected"),
    [((600, 900), 600), ((10,), 300), ((86399,), 3600), ((), 300)],
)
def test_nxdomain_backoff_is_clamped_negative_ttl(settings, negative_ttls, expected):
    answers = [Answer("8.8.8.8", "A", "nxdomain", negative_ttl=t) for t in negative_ttls] or [
        Answer("8.8.8.8", "A", "nxdomain")
    ]
    answers.append(Answer("8.8.8.8", "AAAA", "nxdomain"))

    outcome = classify(answers, settings)

    assert outcome.status == "nxdomain"
    assert outcome.retry_after_seconds == expected


def test_nodata_everywhere(settings):
    answers = [ok("8.8.8.8", "A"), ok("8.8.8.8", "AAAA")]

    assert classify(answers, settings).status == "nodata"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WWW.Example.COM.", "www.example.com"),
        ("  example.com ", "example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", [".", ""])
def test_normalize_rejects_root(raw):
    with pytest.raises(ValueError, match="not a domain name"):
        normalize_domain(raw)
