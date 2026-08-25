from __future__ import annotations

from yamaha_library.dependencies import DependencyResolver, remap_program_numbers
from yamaha_library.parser import YamahaLibraryParser


def test_shared_dependencies_and_transitive_graph(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    selection = DependencyResolver(library).resolve([0, 1])
    assert selection.waveform_programs == (0x00010001, 0x00010002)
    assert selection.shared_waveforms == {0x00010002: 2}
    assert "WFM:0x00010002" in selection.closure
    assert "WIM:0x00010002" in selection.closure
    assert selection.resource_counts == {"ARP": 2, "CRV": 1}
    assert selection.can_export


def test_non_conservative_mode_blocks_unknown_dependency_semantics(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    selection = DependencyResolver(library).resolve([2], conservative=False)
    assert not selection.can_export
    assert any("ARP" in value for value in selection.blockers)


def test_cross_bank_conservative_resource_blocks_export() -> None:
    from tests.synthetic import SyntheticItem, classic_dpfm, make_library

    data = make_library(
        items_by_suffix={
            "PFM": (
                SyntheticItem(0x003F2000, "0:Part", classic_dpfm(), "Safe PFM"),
            ),
            "ARP": (
                SyntheticItem(0x00020000, "Library Arp", b"arp"),
            ),
        }
    )
    library = YamahaLibraryParser().parse_bytes(data, "cross-bank.X8L")
    selection = DependencyResolver(library).resolve([0])
    assert not selection.can_export
    assert any("installed Library" in value for value in selection.blockers)


def test_missing_external_waveform_has_single_actionable_blocker() -> None:
    from tests.synthetic import SyntheticItem, classic_dpfm, make_library

    external_waveform = 0x00020001
    data = make_library(
        family="X7",
        ysfc_version="4.0.1",
        items_by_suffix={
            "PFM": (
                SyntheticItem(
                    0x003F2000,
                    "0:External Part",
                    classic_dpfm(external_waveform),
                    "External Performance",
                    (external_waveform,),
                ),
            ),
            "WFM": (SyntheticItem(0x00010001, "Local Wave", b"metadata"),),
            "WIM": (SyntheticItem(0x00010001, "Local Wave", b"sample"),),
        }
    )
    library = YamahaLibraryParser().parse_bytes(data, "external.X7L")

    selection = DependencyResolver(library).resolve([0])

    assert not selection.can_export
    assert len(selection.blockers) == 1
    blocker = selection.blockers[0]
    assert "Missing external dependency" in blocker
    assert "0x00020001" in blocker
    assert "Library bank 2" in blocker
    assert "Library file" in blocker


def test_program_and_reference_remapping_primitive(synthetic_bytes: bytes) -> None:
    library = YamahaLibraryParser().parse_bytes(synthetic_bytes, "fixture.X8L")
    entry = library.block_map["EPFM"].entries[0]
    mapped = remap_program_numbers(
        (entry,),
        {0x003F2000: 0x003F2007, 0x00010001: 0x00010009},
    )[0]
    assert mapped.program_number == 0x003F2007
    assert mapped.references == (0x00010009, 0x00010002)
    assert mapped.raw != entry.raw
