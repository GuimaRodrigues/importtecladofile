from __future__ import annotations

import json
from pathlib import Path

from yamaha_library.cli import main

from .synthetic import make_motif_library


def test_inspect_and_list_cli(synthetic_file: Path, capsys: object) -> None:
    assert main(["inspect", str(synthetic_file), "--json"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert json.loads(output)["performance_count"] == 3

    assert main(["list", str(synthetic_file), "--json"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert [item["name"] for item in json.loads(output)] == [
        "Performance A",
        "Performance B",
        "Performance C",
    ]


def test_subset_cli(synthetic_file: Path, tmp_path: Path, capsys: object) -> None:
    output = tmp_path / "cli.X8L"
    code = main(
        [
            "subset",
            str(synthetic_file),
            "--performance",
            "Performance B",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert output.exists()
    assert output.with_suffix(".X8L.json").exists()
    assert "Library created" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_subset_cli_accepts_hex_program_number(
    synthetic_file: Path, tmp_path: Path, capsys: object
) -> None:
    output = tmp_path / "by-program.X8L"
    code = main(
        [
            "subset",
            str(synthetic_file),
            "--performance",
            "0x003F2001",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert output.exists()
    assert "Library created" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_subset_cli_can_target_x7(
    synthetic_file: Path, tmp_path: Path, capsys: object
) -> None:
    output = tmp_path / "converted.X7L"
    code = main(
        [
            "subset",
            str(synthetic_file),
            "--performance",
            "Performance B",
            "--target-format",
            "x7l",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert output.exists()
    assert "Library created" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_x0v_cli_lists_voices_and_exports_subset(
    tmp_path: Path, capsys: object
) -> None:
    source = tmp_path / "voices.X0V"
    source.write_bytes(make_motif_library(".X0V"))

    assert main(["list", str(source), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert [item["name"] for item in listed] == ["Motif Voice A", "Motif Drum A"]

    output = tmp_path / "voice-subset.X0V"
    assert (
        main(
            [
                "subset",
                str(source),
                "--performance",
                "0x003F0805",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()
