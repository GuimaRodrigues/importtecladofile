"""Bounds-checked big-endian primitives used by the binary parser."""

from __future__ import annotations

import struct
from collections.abc import Buffer
from typing import cast

from .errors import FormatError


def _check_bounds(data: Buffer, offset: int, size: int, context: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):  # type: ignore[arg-type]
        raise FormatError(
            f"Truncated file while reading {context}: range {offset}:{offset + size}, "
            f"file size {len(data)}."  # type: ignore[arg-type]
        )


def read_u8(data: Buffer, offset: int, context: str = "8-bit integer") -> int:
    _check_bounds(data, offset, 1, context)
    return cast(int, struct.unpack_from(">B", data, offset)[0])


def read_u16be(data: Buffer, offset: int, context: str = "16-bit integer") -> int:
    _check_bounds(data, offset, 2, context)
    return cast(int, struct.unpack_from(">H", data, offset)[0])


def read_u32be(data: Buffer, offset: int, context: str = "32-bit integer") -> int:
    _check_bounds(data, offset, 4, context)
    return cast(int, struct.unpack_from(">I", data, offset)[0])


def read_bytes(data: Buffer, offset: int, size: int, context: str) -> bytes:
    _check_bounds(data, offset, size, context)
    return bytes(data[offset : offset + size])  # type: ignore[index]


def read_c_string(
    data: Buffer,
    offset: int,
    limit: int,
    context: str,
) -> tuple[str, int]:
    """Return a tolerant UTF-8 string and the byte immediately after its NUL."""
    if offset < 0 or limit > len(data) or offset >= limit:  # type: ignore[arg-type]
        raise FormatError(f"Invalid position for {context}: {offset} (limit {limit}).")
    raw = bytes(data[offset:limit])  # type: ignore[index]
    terminator = raw.find(b"\x00")
    if terminator < 0:
        raise FormatError(f"String without a NUL terminator in {context}, position {offset}.")
    value = raw[:terminator].decode("utf-8", errors="replace").rstrip()
    return value, offset + terminator + 1


def pack_u32be(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"Value outside the uint32 range: {value}")
    return struct.pack(">I", value)
