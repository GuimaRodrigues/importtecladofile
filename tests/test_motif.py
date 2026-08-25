from __future__ import annotations

from pathlib import Path

from yamaha_library.model import FileFamily
from yamaha_library.motif import MotifSubsetExporter, MotifSubsetResolver
from yamaha_library.parser import YamahaLibraryParser

from .synthetic import make_motif_library


def test_parse_x0v_voices_and_version_one_entry_layout() -> None:
    library = YamahaLibraryParser().parse_bytes(make_motif_library(".X0V"), "voices.X0V")
    assert library.family is FileFamily.X0
    assert library.header.version == "1.0.0"
    assert library.library_info.raw == b""
    assert library.selectable_kind == "Voice"
    assert [item.name for item in library.selectable_items] == [
        "Motif Voice A",
        "Motif Drum A",
    ]
    first = library.block_map["EVCE"].entries[0]
    assert first.item_size_position == 12
    assert first.data_offset_position == 20
    assert first.program_number_position == 24


def test_x0v_subset_compacts_voice_ids_and_preserves_all_waveforms(tmp_path: Path) -> None:
    source = tmp_path / "source.X0V"
    source_bytes = make_motif_library(".X0V")
    source.write_bytes(source_bytes)
    library = YamahaLibraryParser().parse(source)
    selection = MotifSubsetResolver(library).resolve((0, 1))
    assert selection.target_programs == (0x003F0800, 0x003F2800)

    output = tmp_path / "subset.X0V"
    MotifSubsetExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    assert [item.program_number for item in reopened.voices] == [
        0x003F0800,
        0x003F2800,
    ]
    assert reopened.block_map["EWFM"].count == 2
    assert reopened.block_map["EWIM"].count == 2
    assert source.read_bytes() == source_bytes


def test_x0a_subset_compacts_performances_and_preserves_voices(tmp_path: Path) -> None:
    source = tmp_path / "source.X0A"
    source.write_bytes(make_motif_library(".X0A"))
    library = YamahaLibraryParser().parse(source)
    assert [item.name for item in library.performances] == [
        "Motif Performance A",
        "Motif Performance B",
    ]
    selection = MotifSubsetResolver(library).resolve((1,))
    assert selection.target_programs == (0,)

    output = tmp_path / "subset.X0A"
    MotifSubsetExporter(library).export(selection, output)
    reopened = YamahaLibraryParser().parse(output)
    assert [item.name for item in reopened.performances] == ["Motif Performance B"]
    assert [item.program_number for item in reopened.performances] == [0]
    assert [item.name for item in reopened.voices] == ["Motif Voice A", "Motif Voice B"]
    assert reopened.block_map["EMST"].count == 1


def test_x0_requires_conservative_mode() -> None:
    library = YamahaLibraryParser().parse_bytes(make_motif_library(".X0A"), "source.X0A")
    selection = MotifSubsetResolver(library).resolve((0,), conservative=False)
    assert not selection.can_export
    assert "conservative mode" in selection.blockers[0]
