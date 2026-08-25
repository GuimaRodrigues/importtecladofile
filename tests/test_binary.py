from __future__ import annotations

import pytest

from yamaha_library.binary import read_u16be, read_u32be
from yamaha_library.errors import FormatError


def test_big_endian_reads() -> None:
    data = bytes.fromhex("12 34 89 ab cd ef")
    assert read_u16be(data, 0) == 0x1234
    assert read_u32be(data, 2) == 0x89ABCDEF


def test_bounds_checked_read() -> None:
    with pytest.raises(FormatError, match="Truncated"):
        read_u32be(b"\x00\x01", 0, "test")
