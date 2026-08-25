from __future__ import annotations

import pytest

from yamaha_library.classic_dpfm import (
    remap_classic_waveform_references,
    scan_classic_waveform_references,
)
from yamaha_library.errors import DependencyError, FormatError

from .synthetic import classic_dpfm


def test_scans_and_remaps_classic_waveform_fields() -> None:
    blob = classic_dpfm(0x0002001A, 0x00010004)
    references = scan_classic_waveform_references(blob)
    assert [item.program_number for item in references] == [0x0002001A, 0x00010004]
    rewritten = remap_classic_waveform_references(
        blob, {0x0002001A: 0x00010001, 0x00010004: 0x00010002}
    )
    assert len(rewritten) == len(blob)
    assert [item.program_number for item in scan_classic_waveform_references(rewritten)] == [
        0x00010001,
        0x00010002,
    ]


def test_missing_mapping_and_truncation_are_rejected() -> None:
    with pytest.raises(DependencyError, match="not in"):
        remap_classic_waveform_references(classic_dpfm(0x00020001), {})
    with pytest.raises(FormatError, match="Truncated"):
        scan_classic_waveform_references(b"\x00\x00")
