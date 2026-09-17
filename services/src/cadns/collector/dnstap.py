"""Decode dnstap messages and classify client responses (ADR-2).

Only the few fields the collector needs are decoded, with a small protobuf
reader instead of generated code. Field numbers are from dnstap.proto
(https://github.com/dnstap/dnstap.pb):

    Dnstap:  type = 15 (MESSAGE = 1), message = 14
    Message: type = 1 (CLIENT_RESPONSE = 6), response_time_sec = 12,
             response_time_nsec = 13, response_message = 14 (DNS wire format)
"""

import time
from collections.abc import Iterator
from dataclasses import dataclass

import dns.exception
import dns.flags
import dns.message
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype

DNSTAP_TYPE_MESSAGE = 1
MESSAGE_CLIENT_RESPONSE = 6

HIT, MISS = "hit", "miss"
ADDRESS_TYPES = frozenset({dns.rdatatype.A, dns.rdatatype.AAAA})


class ProtobufError(Exception):
    pass


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ProtobufError("truncated or oversized varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def iter_fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    """(field number, value) pairs: ints for varint/fixed fields, bytes otherwise."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire_type = key >> 3, key & 0x7
        if wire_type == 0:
            value, pos = _varint(buf, pos)
        elif wire_type == 1:
            value, pos = int.from_bytes(buf[pos : pos + 8], "little"), pos + 8
        elif wire_type == 2:
            length, pos = _varint(buf, pos)
            value, pos = buf[pos : pos + length], pos + length
        elif wire_type == 5:
            value, pos = int.from_bytes(buf[pos : pos + 4], "little"), pos + 4
        else:
            raise ProtobufError(f"unsupported wire type {wire_type}")
        if pos > len(buf):
            raise ProtobufError("truncated field")
        yield number, value


@dataclass(frozen=True)
class ClientResponse:
    response_time: float | None
    wire: bytes


def decode_client_response(frame: bytes) -> ClientResponse | None:
    """The CLIENT_RESPONSE carried by a dnstap frame, or None for other messages."""
    dnstap_type = message = None
    for number, value in iter_fields(frame):
        if number == 15:
            dnstap_type = value
        elif number == 14 and isinstance(value, bytes):
            message = value
    if dnstap_type != DNSTAP_TYPE_MESSAGE or message is None:
        return None

    message_type = seconds = wire = None
    nanoseconds = 0
    for number, value in iter_fields(message):
        if number == 1:
            message_type = value
        elif number == 12:
            seconds = value
        elif number == 13 and isinstance(value, int):
            nanoseconds = value
        elif number == 14 and isinstance(value, bytes):
            wire = value
    if message_type != MESSAGE_CLIENT_RESPONSE or wire is None:
        return None
    response_time = seconds + nanoseconds / 1e9 if seconds is not None else None
    return ClientResponse(response_time, wire)


@dataclass(frozen=True)
class Event:
    kind: str  # hit | miss
    name: str
    time: float


def classify(response: ClientResponse, ignore_suffixes: frozenset[str]) -> Event | None:
    """hit: authoritative A/AAAA answer (from DLZ); miss: recursive NOERROR/NXDOMAIN."""
    try:
        message = dns.message.from_wire(response.wire, question_only=True)
    except dns.exception.DNSException:
        return None
    if (
        message.opcode() != dns.opcode.QUERY
        or len(message.question) != 1
        or message.question[0].rdclass != dns.rdataclass.IN
        or message.question[0].rdtype not in ADDRESS_TYPES
    ):
        return None

    name = message.question[0].name.to_text(omit_final_dot=True).lower()
    if is_ignored(name, ignore_suffixes):
        return None

    when = response.response_time or time.time()
    if message.flags & dns.flags.AA:
        return Event(HIT, name, when)
    if message.rcode() in (dns.rcode.NOERROR, dns.rcode.NXDOMAIN):
        return Event(MISS, name, when)
    return None  # SERVFAIL, REFUSED, ...: nothing to learn


def is_ignored(name: str, ignore_suffixes: frozenset[str]) -> bool:
    """Single-label names and names under special-use or documentation domains."""
    if "." not in name:
        return True
    labels = name.split(".")
    return any(".".join(labels[i:]) in ignore_suffixes for i in range(len(labels)))
