import asyncio
import struct

import pytest
from cadns.collector.framestreams import (
    CONTROL_ACCEPT,
    CONTROL_FINISH,
    CONTROL_READY,
    CONTROL_START,
    CONTROL_STOP,
    DNSTAP_CONTENT_TYPE,
    FrameStreamsError,
    decode_control,
    encode_control,
    serve_connection,
)

OTHER = b"protobuf:other.Thing"


class FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


def data_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


async def run(stream: bytes, *, eof=True):
    reader = asyncio.StreamReader()
    reader.feed_data(stream)
    if eof:
        reader.feed_eof()
    writer, frames = FakeWriter(), []
    count = await serve_connection(reader, writer, frames.append)
    return count, frames, bytes(writer.data)


def test_control_frame_round_trip():
    encoded = encode_control(CONTROL_READY, (OTHER, DNSTAP_CONTENT_TYPE))

    assert encoded[:4] == b"\0\0\0\0"  # escape sequence
    (length,) = struct.unpack(">I", encoded[4:8])
    assert decode_control(encoded[8 : 8 + length]) == (CONTROL_READY, [OTHER, DNSTAP_CONTENT_TYPE])


async def test_bidirectional_session():
    stream = (
        encode_control(CONTROL_READY, (OTHER, DNSTAP_CONTENT_TYPE))
        + encode_control(CONTROL_START, (DNSTAP_CONTENT_TYPE,))
        + data_frame(b"one")
        + data_frame(b"two")
        + encode_control(CONTROL_STOP)
    )

    count, frames, written = await run(stream)

    assert (count, frames) == (2, [b"one", b"two"])
    assert written == encode_control(CONTROL_ACCEPT, (DNSTAP_CONTENT_TYPE,)) + encode_control(
        CONTROL_FINISH
    )


async def test_unidirectional_stream_until_disconnect():
    stream = encode_control(CONTROL_START, (DNSTAP_CONTENT_TYPE,)) + data_frame(b"x")
    stream += data_frame(b"truncated")[:6]  # writer died mid-frame

    count, frames, written = await run(stream)

    assert (count, frames, written) == (1, [b"x"], b"")


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (encode_control(CONTROL_READY, (OTHER,)), "does not offer"),
        (encode_control(CONTROL_START, (OTHER,)), "unexpected content type"),
        (data_frame(b"early"), "before START"),
        (b"\0\0\0\0" + struct.pack(">I", 10_000), "exceeds limit"),
        (struct.pack(">I", 1 << 30), "exceeds limit"),
        (encode_control(0x42), "unexpected control frame"),
    ],
)
async def test_protocol_violations(stream, message):
    with pytest.raises(FrameStreamsError, match=message):
        await run(stream)


def test_truncated_control_field():
    payload = struct.pack(">III", CONTROL_READY, 1, 100) + b"short"

    with pytest.raises(FrameStreamsError, match="truncated"):
        decode_control(payload)
