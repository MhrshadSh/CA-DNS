import pytest
from cadns.collector.dnstap import (
    HIT,
    MISS,
    ClientResponse,
    ProtobufError,
    classify,
    decode_client_response,
    is_ignored,
    iter_fields,
)
from cadns.config import Settings
from conftest import dnstap_frames


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number: int, wire_type: int, payload: bytes) -> bytes:
    key = varint(number << 3 | wire_type)
    return key + (varint(len(payload)) + payload if wire_type == 2 else payload)


def dnstap(message_type: int, wire: bytes = b"") -> bytes:
    message = field(1, 0, varint(message_type)) + field(14, 2, wire)
    return field(15, 0, varint(1)) + field(14, 2, message)


def events(ignore=frozenset()):
    result = []
    for frame in dnstap_frames():
        response = decode_client_response(frame)
        assert response is not None
        event = classify(response, ignore)
        result.append(event and (event.kind, event.name))
    return result


def test_recorded_bind_frames_are_classified():
    assert events() == [
        (HIT, "www.example.test"),  # A, answered from DLZ
        (HIT, "www.example.test"),  # AAAA, answered from DLZ
        (MISS, "example.com"),  # recursive
        (MISS, "cadns-nonexistent-fixture-name.com"),  # recursive NXDOMAIN
        None,  # TXT
        None,  # TXT, even though authoritative
    ]


def test_default_ignore_list_drops_special_use_and_documentation_names():
    assert events(Settings().ignore_suffixes) == [
        None,
        None,
        None,
        (MISS, "cadns-nonexistent-fixture-name.com"),
        None,
        None,
    ]


def test_recorded_frames_carry_response_time_with_nanoseconds():
    times = [decode_client_response(f).response_time for f in dnstap_frames()]

    assert all(t > 1_700_000_000 for t in times)
    assert any(t != int(t) for t in times)  # sub-second precision from response_time_nsec
    assert times == sorted(times)


def test_other_message_types_are_skipped():
    wire = decode_client_response(dnstap_frames()[0]).wire

    assert decode_client_response(dnstap(5, wire)) is None  # CLIENT_QUERY
    assert decode_client_response(dnstap(6, wire)) is not None  # CLIENT_RESPONSE


def test_unknown_fields_of_every_wire_type_are_skipped():
    frame = (
        field(1, 2, b"identity")
        + field(4, 5, b"\x01\x02\x03\x04")  # fixed32
        + field(5, 1, b"\0" * 8)  # fixed64
        + dnstap(6, b"wire")
    )

    assert decode_client_response(frame).wire == b"wire"


@pytest.mark.parametrize("frame", [b"\x80", b"\x7a\x05ab", b"\x0b"])
def test_malformed_protobuf_raises(frame):
    with pytest.raises(ProtobufError):
        list(iter_fields(frame))


def test_malformed_dns_message_is_ignored():
    assert classify(ClientResponse(None, b"\x00\x01garbage"), frozenset()) is None


@pytest.mark.parametrize(
    ("name", "ignored"),
    [
        ("localhost", True),
        ("printer", True),  # single label
        ("a.test", True),
        ("www.example.com", True),
        ("x.1.168.192.in-addr.arpa", True),
        ("foo.mytest", False),  # not a label boundary
        ("example.com.au", False),
        ("www.youtube.com", False),
    ],
)
def test_ignore_suffixes(name, ignored):
    assert is_ignored(name, Settings().ignore_suffixes) is ignored
