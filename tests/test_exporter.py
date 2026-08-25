from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from yamaha_library.classic_dpfm import scan_classic_waveform_references
from yamaha_library.dependencies import DependencyResolver
from yamaha_library.errors import ExportError
from yamaha_library.exporter import LibraryExporter
from yamaha_library.model import FileFamily
from yamaha_library.parser import YamahaLibraryParser

from .synthetic import SyntheticItem, classic_dpfm, make_library


def test_subset_round_trip_and_original_unchanged(synthetic_file: Path, tmp_path: Path) -> None:
    before = synthetic_file.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    library = YamahaLibraryParser().parse(synthetic_file)
    selection = DependencyResolver(library).resolve([1])
    output = tmp_path / "subset.X8L"

    result = LibraryExporter(library).export(selection, output)

    assert result.output_path == output.resolve()
    assert output.exists()
    assert result.report_path.exists()
    reopened = YamahaLibraryParser().parse(output)
    assert [item.name for item in reopened.performances] == ["Performance B"]
    assert [entry.program_number for entry in reopened.block_map["EWFM"].entries] == [
        0x00010001
    ]
    assert [entry.program_number for entry in reopened.block_map["EWIM"].entries] == [
        0x00010001
    ]
    assert reopened.performances[0].entry.references == (0x00010001,)
    dpfm_record = reopened.block_map["DPFM"].data_records[0]
    dpfm = reopened.source_range(dpfm_record.source_offset + 8, dpfm_record.payload_size)
    assert [item.program_number for item in scan_classic_waveform_references(dpfm)] == [
        0x00010001
    ]
    assert len(reopened.block_map["EARP"].entries) == 2
    assert len(reopened.block_map["ECRV"].entries) == 1
    assert "ELST" not in reopened.block_map
    assert hashlib.sha256(synthetic_file.read_bytes()).hexdigest() == before_hash
    assert synthetic_file.read_bytes() == before

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["selection"]["performances"] == ["Performance B"]
    assert report["hardware_validation"] == "not_performed"
    assert report["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_every_rewritten_offset_is_in_bounds(synthetic_file: Path, tmp_path: Path) -> None:
    library = YamahaLibraryParser().parse(synthetic_file)
    selection = DependencyResolver(library).resolve([0, 2])
    output = tmp_path / "offsets.X8L"
    LibraryExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    for entry_block in (block for block in reopened.blocks if block.is_entry_block):
        data_block = reopened.block_map["D" + entry_block.suffix]
        for entry, record in zip(entry_block.entries, data_block.data_records, strict=True):
            assert entry.data_offset == record.source_offset - data_block.source_offset
            assert record.source_offset + record.total_size <= reopened.source_size


def test_output_family_and_in_place_writes_are_rejected(synthetic_file: Path, tmp_path: Path) -> None:
    library = YamahaLibraryParser().parse(synthetic_file)
    selection = DependencyResolver(library).resolve([2])
    exporter = LibraryExporter(library)
    with pytest.raises(ExportError, match="must use .X8L"):
        exporter.export(selection, tmp_path / "wrong.X7L")
    with pytest.raises(ExportError, match="source"):
        exporter.export(selection, synthetic_file, overwrite=True)


def test_existing_destination_requires_force(synthetic_file: Path, tmp_path: Path) -> None:
    library = YamahaLibraryParser().parse(synthetic_file)
    selection = DependencyResolver(library).resolve([2])
    output = tmp_path / "exists.X8L"
    output.write_bytes(b"keep")
    with pytest.raises(ExportError, match="already exists"):
        LibraryExporter(library).export(selection, output)
    assert output.read_bytes() == b"keep"


def test_unknown_opaque_chunk_is_preserved_in_conservative_mode(tmp_path: Path) -> None:
    source = tmp_path / "unknown.X8L"
    source.write_bytes(make_library(opaque_blocks=(("XZZZ", b"opaque-payload"),)))
    library = YamahaLibraryParser().parse(source)
    assert any("XZZZ" in warning for warning in library.warnings)
    selection = DependencyResolver(library).resolve([2])
    output = tmp_path / "unknown-subset.X8L"
    LibraryExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    assert "XZZZ" in reopened.block_map
    source_block = library.block_map["XZZZ"]
    output_block = reopened.block_map["XZZZ"]
    assert library.source_range(source_block.source_offset, source_block.total_size) == (
        reopened.source_range(output_block.source_offset, output_block.total_size)
    )


def test_x7_input_produces_x7_library(tmp_path: Path) -> None:
    source = tmp_path / "source.X7U"
    source.write_bytes(make_library(family="X7"))
    library = YamahaLibraryParser().parse(source)
    selection = DependencyResolver(library).resolve([2])
    output = tmp_path / "subset.X7L"
    LibraryExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    assert reopened.family.value == "X7"
    assert reopened.header.version == "4.0.5"
    assert [item.name for item in reopened.performances] == ["Performance C"]


def test_legacy_x7_401_is_exported_without_inventing_entry_ids(tmp_path: Path) -> None:
    source = tmp_path / "legacy.X7L"
    source.write_bytes(make_library(family="X7", ysfc_version="4.0.1"))
    library = YamahaLibraryParser().parse(source)
    assert not library.header.has_entry_ids
    original_header_value = library.header.next_timestamp

    selection = DependencyResolver(library).resolve([1])
    output = tmp_path / "legacy-subset.X7L"
    LibraryExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)

    assert reopened.header.version == "4.0.1"
    assert reopened.header.next_timestamp == original_header_value
    assert [item.name for item in reopened.performances] == ["Performance B"]
    assert reopened.performances[0].program_number == 0x003F2000


def test_x8_can_be_exported_as_modern_x7_container(
    synthetic_file: Path, tmp_path: Path
) -> None:
    library = YamahaLibraryParser().parse(synthetic_file)
    selection = DependencyResolver(library).resolve([1])
    output = tmp_path / "converted.X7L"

    result = LibraryExporter(library, target_family=FileFamily.X7).export(
        selection, output
    )
    reopened = YamahaLibraryParser().parse(output)

    assert reopened.family is FileFamily.X7
    assert reopened.header.version == "4.0.5"
    assert [item.name for item in reopened.performances] == ["Performance B"]
    assert result.report["output"]["family"] == "X7"  # type: ignore[index]
    assert result.report["output"]["converted_from_source_family"] is True  # type: ignore[index]


def test_legacy_x7_can_be_exported_as_x8_with_entry_ids(tmp_path: Path) -> None:
    source = tmp_path / "legacy.X7L"
    source.write_bytes(make_library(family="X7", ysfc_version="4.0.1"))
    library = YamahaLibraryParser().parse(source)
    selection = DependencyResolver(library).resolve([1])
    source_entry_size = len(library.performances[1].entry.raw)
    output = tmp_path / "converted.X8L"
    exporter = LibraryExporter(library, target_family=FileFamily.X8)

    result = exporter.export(selection, output)
    reopened = YamahaLibraryParser().parse(output)

    assert reopened.family is FileFamily.X8
    assert reopened.header.version == "5.0.1"
    assert reopened.header.has_entry_ids
    assert reopened.performances[0].entry.timestamp > 0
    assert len(reopened.performances[0].entry.raw) == source_entry_size + 4
    assert result.output_size == exporter.estimate_size(selection)


def test_library_bank_performance_and_waveform_are_remapped(tmp_path: Path) -> None:
    source = tmp_path / "backup.X8A"
    source.write_bytes(
        make_library(
            items_by_suffix={
                "PFM": (
                    SyntheticItem(
                        0x003F2815,
                        "0:Library Part",
                        classic_dpfm(0x0002001A),
                        "Library Performance",
                        (0x0002001A,),
                    ),
                ),
                "WFM": (
                    SyntheticItem(0x0002001A, "0:Library Wave", b"metadata"),
                ),
                "WIM": (
                    SyntheticItem(0x0002001A, "0:Library Wave", b"sample-data"),
                ),
            }
        )
    )
    library = YamahaLibraryParser().parse(source)
    selection = DependencyResolver(library).resolve([0])
    assert selection.can_export
    output = tmp_path / "remapped.X8L"
    LibraryExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    assert reopened.performances[0].program_number == 0x003F2000
    assert reopened.performances[0].entry.references == (0x00010001,)
    assert reopened.block_map["EWFM"].entries[0].program_number == 0x00010001
    record = reopened.block_map["DPFM"].data_records[0]
    blob = reopened.source_range(record.source_offset + 8, record.payload_size)
    assert [item.program_number for item in scan_classic_waveform_references(blob)] == [
        0x00010001
    ]
