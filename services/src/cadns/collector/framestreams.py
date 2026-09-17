"""Frame Streams reader (https://github.com/farsightsec/fstrm).

BIND is the writer and connects to our unix socket. Bi-directional handshake:

    writer: READY(content types)   reader: ACCEPT(content type)
    writer: START(content type)
    writer: data frames ...
    writer: STOP                   reader: FINISH

Frames are length-prefixed (32-bit big endian). A zero length is an escape:
a control frame follows, itself length-prefixed, starting with its 32-bit
type and followed by (field type, length, value) fields.
"""

import asyncio
import struct
from collections.abc import Callable

CONTROL_ACCEPT = 0x01
CONTROL_START = 0x02
CONTROL_STOP = 0x03
CONTROL_READY = 0x04
CONTROL_FINISH = 0x05
FIELD_CONTENT_TYPE = 0x01

CONTROL_FRAME_MAX = 512
DATA_FRAME_MAX = 1 << 20  # a DNS message is at most 64 KiB; dnstap adds little

DNSTAP_CONTENT_TYPE = b"protobuf:dnstap.Dnstap"

_U32 = struct.Struct(">I")
_U32_PAIR = struct.Struct(">II")


class FrameStreamsError(Exception):
    pass


def encode_control(control_type: int, content_types: tuple[bytes, ...] = ()) -> bytes:
    payload = _U32.pack(control_type) + b"".join(
        _U32.pack(FIELD_CONTENT_TYPE) + _U32.pack(len(ct)) + ct for ct in content_types
    )
    return _U32.pack(0) + _U32.pack(len(payload)) + payload


def decode_control(payload: bytes) -> tuple[int, list[bytes]]:
    if len(payload) < 4:
        raise FrameStreamsError("control frame too short")
    (control_type,) = _U32.unpack_from(payload, 0)
    pos, content_types = 4, []
    while pos < len(payload):
        if pos + 8 > len(payload):
            raise FrameStreamsError("truncated control field")
        field_type, length = _U32_PAIR.unpack_from(payload, pos)
        pos += 8
        if pos + length > len(payload):
            raise FrameStreamsError("truncated control field value")
        if field_type == FIELD_CONTENT_TYPE:
            content_types.append(payload[pos : pos + length])
        pos += length
    return control_type, content_types


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int | None, bytes]:
    """(control type, content-type bytes) for control frames, (None, data) otherwise."""
    (length,) = _U32.unpack(await reader.readexactly(4))
    if length > 0:
        if length > DATA_FRAME_MAX:
            raise FrameStreamsError(f"data frame of {length} bytes exceeds limit")
        return None, await reader.readexactly(length)
    (length,) = _U32.unpack(await reader.readexactly(4))
    if length > CONTROL_FRAME_MAX:
        raise FrameStreamsError(f"control frame of {length} bytes exceeds limit")
    control_type, content_types = decode_control(await reader.readexactly(length))
    return control_type, b"\n".join(content_types)


async def serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    on_frame: Callable[[bytes], None],
    content_type: bytes = DNSTAP_CONTENT_TYPE,
) -> int:
    """Run one Frame Streams session; returns the number of data frames.

    Accepts bi-directional (READY first) and uni-directional (START first)
    writers. Returns normally when the writer stops or disconnects.
    """
    frames = 0
    started = False
    try:
        while True:
            control, data = await _read_frame(reader)
            if control is None:
                if not started:
                    raise FrameStreamsError("data frame before START")
                on_frame(data)
                frames += 1
            elif control == CONTROL_READY:
                if content_type not in data.split(b"\n"):
                    raise FrameStreamsError(f"writer does not offer {content_type!r}")
                writer.write(encode_control(CONTROL_ACCEPT, (content_type,)))
                await writer.drain()
            elif control == CONTROL_START:
                if data and data != content_type:
                    raise FrameStreamsError(f"unexpected content type {data!r}")
                started = True
            elif control == CONTROL_STOP:
                writer.write(encode_control(CONTROL_FINISH))
                await writer.drain()
                return frames
            else:
                raise FrameStreamsError(f"unexpected control frame type {control}")
    except asyncio.IncompleteReadError:
        return frames  # writer went away (e.g. named restarting)
