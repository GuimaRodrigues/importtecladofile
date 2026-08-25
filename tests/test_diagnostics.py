from __future__ import annotations

from pathlib import Path

from yamaha_library.diagnostics import probe_ysfc_file

from .synthetic import make_library


def test_probe_reads_legacy_header_and_catalogue_without_payload(tmp_path: Path) -> None:
    source = tmp_path / "legacy.X7L"
    source.write_bytes(make_library(family="X7", ysfc_version="4.0.1"))
    result = probe_ysfc_file(source)
    assert result["name"] == "legacy.X7L"
    assert result["ysfc_version"] == "4.0.1"
    assert result["signature"] == "YAMAHA-YSFC"
    catalogue = result["catalogue"]
    assert isinstance(catalogue, list)
    assert catalogue[0]["id"] == "EPFM"
    assert "probe_error" not in result


def test_probe_reports_truncated_header(tmp_path: Path) -> None:
    source = tmp_path / "broken.X7L"
    source.write_bytes(b"YAMAHA-YSFC")
    result = probe_ysfc_file(source)
    assert result["header_bytes_read"] == 11
    assert result["probe_error"] == "Header is shorter than 64 bytes."
