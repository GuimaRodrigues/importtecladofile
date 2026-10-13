from __future__ import annotations

import json
import mmap
from pathlib import Path

import pytest

from yamaha_library.cli import main
from yamaha_library.errors import FormatError
from yamaha_library.npno import NpnoParser, decode_stroke
from yamaha_library.npno_exporter import NpnoResolver, NpnoX8lExporter
from yamaha_library.parser import YamahaLibraryParser

from .synthetic import make_npno


def test_npno_parser_and_decoder(tmp_path: Path) -> None:
    source = tmp_path / "piano.npno"
    source.write_bytes(make_npno(trim_db=-12, gain_tenths_db=25))

    library = NpnoParser().parse(source)
    assert library.name == "Test Piano"
    assert library.variant == "Small"
    assert library.channels == 1
    assert library.header.checksum_ok
    assert len(library.attack_strokes) == 1
    assert not library.release_strokes
    assert library.strokes[0].trim_tenths_db == -12
    assert library.gain_tenths_db == 25

    with source.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as view:
        audio = decode_stroke(view, library.strokes[0], library.channels)
    assert audio.shape == (956, 1)
    assert not audio.any()


def test_npno_bad_checksum_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "bad.npno"
    damaged = bytearray(make_npno())
    damaged[-1] = 1
    source.write_bytes(damaged)
    with pytest.raises(FormatError, match="checksum"):
        NpnoParser().parse(source)


def test_npno_to_x8l_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "piano.npno"
    source.write_bytes(make_npno())
    library = NpnoParser().parse(source)
    selection = NpnoResolver(library).resolve([0])
    destination = tmp_path / "piano.X8L"

    result = NpnoX8lExporter(library).export(selection, destination)

    converted = YamahaLibraryParser().parse(destination)
    assert [performance.name for performance in converted.performances] == ["Test Piano"]
    assert len(converted.block_map["EWFM"].entries) == 1
    assert result.output_size == destination.stat().st_size
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["conversion"]["attack_strokes"] == 1
    assert report["output"]["family"] == "X8"


def test_npno_cli_inspect_and_subset(tmp_path: Path, capsys: object) -> None:
    source = tmp_path / "piano.npno"
    source.write_bytes(make_npno())
    assert main(["inspect", str(source), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert inspected["family"] == "NPNO"
    assert inspected["stroke_count"] == 1

    destination = tmp_path / "piano.X8L"
    assert (
        main(
            [
                "subset",
                str(source),
                "--performance",
                "Test Piano",
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    assert destination.exists()
