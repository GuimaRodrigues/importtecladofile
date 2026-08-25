from __future__ import annotations

import struct

import pytest

from yamaha_library.errors import FormatError, UnsupportedFormatError
from yamaha_library.parser import YamahaLibraryParser

from .synthetic import make_library


def test_catalogue_and_library_info(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    assert library.header.version == "5.0.1"
    assert library.header.catalogue_size == len(library.catalogue) * 8
    assert library.library_info.raw == b"\xFF" * 80 + b"\x00"
    assert library.library_info.occupied_slots == ()
    assert library.chunk_ids == (
        "EPFM",
        "EWFM",
        "EWIM",
        "EARP",
        "ECRV",
        "ELST",
        "DPFM",
        "DWFM",
        "DWIM",
        "DARP",
        "DCRV",
        "DLST",
    )
    offsets = [item.offset for item in library.catalogue]
    assert offsets == sorted(offsets)
    assert len(offsets) == len(set(offsets))


def test_entry_and_data_chunks_are_linked(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    entry_block = library.block_map["EPFM"]
    data_block = library.block_map["DPFM"]
    assert entry_block.count == data_block.count == 3
    assert entry_block.entries[0].title == "Performance A"
    assert entry_block.entries[0].references == (0x00010001, 0x00010002)
    assert entry_block.entries[0].item_size == data_block.data_records[0].payload_size
    assert entry_block.entries[0].data_offset == (
        data_block.data_records[0].source_offset - data_block.source_offset
    )


@pytest.mark.parametrize("cut", [0, 1, 15, 63, 64, 100, -1, -7])
def test_truncated_files_are_rejected(synthetic_bytes: bytes, cut: int) -> None:
    truncated = synthetic_bytes[:cut]
    with pytest.raises(FormatError):
        YamahaLibraryParser().parse_bytes(truncated, "truncated.X8L")


def test_bad_signature_is_rejected(synthetic_bytes: bytes) -> None:
    damaged = bytearray(synthetic_bytes)
    damaged[0] = 0
    with pytest.raises(FormatError, match="signature"):
        YamahaLibraryParser().parse_bytes(bytes(damaged), "bad.X8L")


def test_catalogue_size_must_be_multiple_of_eight(synthetic_bytes: bytes) -> None:
    damaged = bytearray(synthetic_bytes)
    struct.pack_into(">I", damaged, 32, 7)
    with pytest.raises(FormatError, match="multiple of 8"):
        YamahaLibraryParser().parse_bytes(bytes(damaged), "bad.X8L")


def test_entry_offset_mismatch_is_rejected(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    damaged = bytearray(synthetic_bytes)
    first_entry = library.block_map["EPFM"].entries[0]
    struct.pack_into(">I", damaged, first_entry.source_offset + 12, 13)
    with pytest.raises(FormatError, match="expected"):
        YamahaLibraryParser().parse_bytes(bytes(damaged), "bad.X8L")


def test_extension_and_version_must_match(synthetic_bytes: bytes) -> None:
    with pytest.raises(FormatError, match="header"):
        YamahaLibraryParser().parse_bytes(synthetic_bytes, "wrong.X7L")
    with pytest.raises(UnsupportedFormatError, match="extension"):
        YamahaLibraryParser().parse_bytes(synthetic_bytes, "wrong.bin")


@pytest.mark.parametrize("version", ["4.0.1", "4.0.2", "4.0.3", "4.0.4", "4.0.5"])
def test_montage_x7_entry_layout_is_selected_by_version(version: str) -> None:
    data = make_library(family="X7", ysfc_version=version)
    library = YamahaLibraryParser().parse_bytes(data, "legacy.X7L")
    assert library.header.version == version
    assert library.header.has_entry_ids is (version >= "4.0.3")
    assert [item.name for item in library.performances] == [
        "Performance A",
        "Performance B",
        "Performance C",
    ]
    expected_name_offset = 30 if version >= "4.0.3" else 26
    first_entry = library.block_map["EPFM"].entries[0]
    assert first_entry.raw[expected_name_offset:].startswith(b"0:Part A\x00")
