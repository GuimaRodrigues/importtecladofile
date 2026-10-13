"""Command-line interface for inspection, dependency analysis, and export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from .dependencies import DependencyResolver, DependencySelection
from .errors import DependencyError, YamahaLibraryError
from .exporter import LibraryExporter
from .loader import SourceLibrary, load_library
from .model import FileFamily, ParsedLibrary
from .motif import MotifSelection, MotifSubsetExporter, MotifSubsetResolver
from .npno import NpnoLibrary
from .npno_exporter import NpnoResolver, prepare_exporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yamaha-library",
        description="Inspect Yamaha files and create conservative X0/X7/X8 subsets.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subcommands.add_parser("inspect", help="validate and summarize a file")
    inspect_parser.add_argument("file", type=Path)
    inspect_parser.add_argument("--json", action="store_true", help="output JSON")
    inspect_parser.set_defaults(handler=_inspect)

    list_parser = subcommands.add_parser("list", help="list all selectable items")
    list_parser.add_argument("file", type=Path)
    list_parser.add_argument("--json", action="store_true", help="output JSON")
    list_parser.set_defaults(handler=_list)

    dependency_parser = subcommands.add_parser(
        "dependencies", help="show dependencies for a Performance or Voice"
    )
    dependency_parser.add_argument("file", type=Path)
    dependency_parser.add_argument("--performance", required=True)
    dependency_parser.add_argument("--json", action="store_true", help="output JSON")
    dependency_parser.add_argument(
        "--no-conservative",
        action="store_true",
        help="do not preserve uncertain resources (export may be blocked)",
    )
    dependency_parser.set_defaults(handler=_dependencies)

    subset_parser = subcommands.add_parser("subset", help="export a new Yamaha Library subset")
    subset_parser.add_argument("file", type=Path)
    subset_parser.add_argument(
        "--performance", action="append", required=True, help="may be repeated"
    )
    subset_parser.add_argument("--output", required=True, type=Path)
    subset_parser.add_argument(
        "--target-format",
        type=str.upper,
        choices=("X7L", "X8L"),
        help="convert the output container to MONTAGE X7L or MODX X8L",
    )
    subset_parser.add_argument("--force", action="store_true", help="overwrite existing output")
    subset_parser.add_argument(
        "--no-conservative",
        action="store_true",
        help="do not preserve uncertain resources (export may be blocked)",
    )
    subset_parser.set_defaults(handler=_subset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except YamahaLibraryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: operation cancelled", file=sys.stderr)
        return 130


def _load(path: Path) -> SourceLibrary:
    return load_library(path)


def _inspect(arguments: argparse.Namespace) -> int:
    library = _load(arguments.file)
    if isinstance(library, NpnoLibrary):
        payload = {
            "file": str(arguments.file.resolve()),
            "size": library.source_size,
            "family": "NPNO",
            "version": library.header.version,
            "input_extension": library.input_extension,
            "name": library.name,
            "variant": library.variant,
            "channels": library.channels,
            "stroke_count": len(library.strokes),
            "attack_strokes": len(library.attack_strokes),
            "resonance_strokes": len(library.resonance_strokes),
            "release_strokes": len(library.release_strokes),
            "checksum_verified": library.header.checksum_ok,
            "selectable_kind": library.selectable_kind,
            "selectable_count": 1,
            "warnings": list(library.warnings),
        }
        if arguments.json:
            _print_json(payload)
        else:
            print(f"File: {payload['file']}")
            print(f"Format: Nord Piano Library / {library.header.version}")
            print(f"Instrument: {library.selectable_items[0].name}")
            print(f"Size: {library.source_size} bytes")
            print(
                f"Strokes: {len(library.strokes)} "
                f"({len(library.attack_strokes)} attack, "
                f"{len(library.resonance_strokes)} resonance, "
                f"{len(library.release_strokes)} release)"
            )
            for warning in library.warnings:
                print(f"Warning: {warning}")
        return 0
    payload = {
        "file": str(arguments.file.resolve()),
        "size": library.source_size,
        "family": library.family.value,
        "version": library.header.version,
        "input_extension": library.input_extension,
        "library_slots": [
            {"slot": item.slot_number, "name": item.name}
            for item in library.library_info.occupied_slots
        ],
        "performance_count": len(library.performances),
        "voice_count": len(library.voices),
        "selectable_kind": library.selectable_kind,
        "selectable_count": len(library.selectable_items),
        "chunks": [
            {
                "id": block.chunk_id,
                "offset": block.source_offset,
                "size": block.total_size,
                "count": block.count,
            }
            for block in library.blocks
        ],
        "warnings": list(library.warnings),
    }
    if arguments.json:
        _print_json(payload)
    else:
        print(f"File: {payload['file']}")
        print(f"Format: {library.family.value} / YSFC {library.header.version}")
        print(f"Size: {library.source_size} bytes")
        print(f"{_kind_plural(library)}: {len(library.selectable_items)}")
        print("Chunks: " + ", ".join(library.chunk_ids))
        if library.library_info.occupied_slots:
            print(
                "Installed Libraries: "
                + ", ".join(
                    f"slot {slot.slot_number - 1}: {slot.name}"
                    for slot in library.library_info.occupied_slots
                )
            )
        for warning in library.warnings:
            print(f"Warning: {warning}")
    return 0


def _list(arguments: argparse.Namespace) -> int:
    library = _load(arguments.file)
    payload = [
        {
            "index": performance.index,
            "program": f"0x{performance.program_number:08X}",
            "name": performance.name,
        }
        for performance in library.selectable_items
    ]
    if arguments.json:
        _print_json(payload)
    else:
        for item in payload:
            print(f"{item['index']:4}  {item['program']}  {item['name']}")
    return 0


def _dependencies(arguments: argparse.Namespace) -> int:
    library = _load(arguments.file)
    if isinstance(library, NpnoLibrary):
        _npno_indices_by_names(library, [arguments.performance])
        selection = NpnoResolver(library).resolve([0])
        estimated_size = prepare_exporter(library, selection).estimate_size(selection)
        payload = {
            "instrument": library.selectable_items[0].name,
            "attack_strokes": len(library.attack_strokes),
            "release_strokes": len(library.release_strokes),
            "resonance_strokes_omitted": len(library.resonance_strokes),
            "waveforms": len(selection.waveform_plans),
            "estimated_size": estimated_size,
            "can_export": selection.can_export,
            "warnings": list(selection.warnings),
            "blockers": list(selection.blockers),
        }
        if arguments.json:
            _print_json(payload)
        else:
            print(f"Instrument: {payload['instrument']}")
            print(f"Generated Waveforms: {payload['waveforms']}")
            print(f"Estimated X8L size: {estimated_size} bytes")
            for warning in selection.warnings:
                print(f"Warning: {warning}")
            for blocker in selection.blockers:
                print(f"Blocker: {blocker}")
        return 0 if selection.can_export else 3
    if library.family is FileFamily.X0:
        indices = _motif_indices_by_names(library, [arguments.performance])
        motif_selection = MotifSubsetResolver(library).resolve(
            indices, conservative=not arguments.no_conservative
        )
        estimated_size = MotifSubsetExporter(library).estimate_size(motif_selection)
        payload = _motif_selection_payload(library, motif_selection, estimated_size)
        if arguments.json:
            _print_json(payload)
        else:
            print(f"{motif_selection.item_kind}: {library.selectable_items[indices[0]].name}")
            print(f"Preserved resources: {sum(motif_selection.preserved_counts.values())}")
            print(f"Estimated size: {estimated_size} bytes")
            for warning in motif_selection.warnings:
                print(f"Warning: {warning}")
            for blocker in motif_selection.blockers:
                print(f"Blocker: {blocker}")
        return 0 if motif_selection.can_export else 3

    resolver = DependencyResolver(library)
    indices = resolver.performance_indices_by_names([arguments.performance])
    dependency_selection = resolver.resolve(
        indices, conservative=not arguments.no_conservative
    )
    payload = _selection_payload(
        library,
        dependency_selection,
        LibraryExporter(library).estimate_size(dependency_selection),
    )
    if arguments.json:
        _print_json(payload)
    else:
        print(f"Performance: {library.performances[indices[0]].name}")
        print(f"Required Waveforms: {dependency_selection.waveform_count}")
        print(f"Sample payloads: {dependency_selection.sample_count}")
        print(f"Shared Waveforms: {len(dependency_selection.shared_waveforms)}")
        print(
            "Estimated size: "
            f"{LibraryExporter(library).estimate_size(dependency_selection)} bytes"
        )
        for warning in dependency_selection.warnings:
            print(f"Warning: {warning}")
        for blocker in dependency_selection.blockers:
            print(f"Blocker: {blocker}")
    return 0 if dependency_selection.can_export else 3


def _subset(arguments: argparse.Namespace) -> int:
    library = _load(arguments.file)
    if isinstance(library, NpnoLibrary):
        if arguments.target_format not in {None, "X8L"}:
            raise DependencyError("NPNO conversion supports MODX/MODX+ X8L output only.")
        _npno_indices_by_names(library, arguments.performance)
        npno_selection = NpnoResolver(library).resolve([0])
        result = prepare_exporter(library, npno_selection).export(
            npno_selection, arguments.output, overwrite=arguments.force
        )
    elif library.family is FileFamily.X0:
        if arguments.target_format is not None:
            raise DependencyError(
                "X0A/X0V to X7L/X8L conversion is not supported; "
                "use the original MOTIF XS format."
            )
        indices = _motif_indices_by_names(library, arguments.performance)
        motif_selection = MotifSubsetResolver(library).resolve(
            indices, conservative=not arguments.no_conservative
        )
        result = MotifSubsetExporter(library).export(
            motif_selection, arguments.output, overwrite=arguments.force
        )
    else:
        target_family = (
            FileFamily(arguments.target_format[:2])
            if arguments.target_format is not None
            else None
        )
        resolver = DependencyResolver(library)
        indices = resolver.performance_indices_by_names(arguments.performance)
        dependency_selection = resolver.resolve(
            indices, conservative=not arguments.no_conservative
        )
        result = LibraryExporter(library, target_family=target_family).export(
            dependency_selection, arguments.output, overwrite=arguments.force
        )
    print(f"Library created: {result.output_path}")
    print(f"JSON report: {result.report_path}")
    print(f"Size: {result.output_size} bytes")
    print(f"SHA-256: {result.sha256}")
    return 0


def _selection_payload(
    library: ParsedLibrary,
    selection: DependencySelection,
    estimated_size: int,
) -> dict[str, object]:
    return {
        "performances": [
            library.performances[index].name for index in selection.performance_indices
        ],
        "waveform_programs": [f"0x{value:08X}" for value in selection.waveform_programs],
        "waveform_count": selection.waveform_count,
        "waveform_image_sample_payload_count": selection.sample_count,
        "shared_waveforms": {
            f"0x{key:08X}": count for key, count in selection.shared_waveforms.items()
        },
        "conservatively_preserved_resources": selection.resource_counts,
        "estimated_size": estimated_size,
        "can_export": selection.can_export,
        "warnings": list(selection.warnings),
        "blockers": list(selection.blockers),
    }


def _motif_selection_payload(
    library: ParsedLibrary,
    selection: MotifSelection,
    estimated_size: int,
) -> dict[str, object]:
    return {
        "kind": selection.item_kind,
        "items": [library.selectable_items[index].name for index in selection.item_indices],
        "program_remap": {
            f"0x{source:08X}": f"0x{target:08X}"
            for source, target in selection.program_map.items()
        },
        "preserved_resources": selection.preserved_counts,
        "estimated_size": estimated_size,
        "can_export": selection.can_export,
        "warnings": list(selection.warnings),
        "blockers": list(selection.blockers),
    }


def _motif_indices_by_names(
    library: ParsedLibrary, names: list[str] | tuple[str, ...]
) -> tuple[int, ...]:
    items = library.selectable_items
    by_name: dict[str, list[int]] = {}
    by_program = {item.program_number: item.index for item in items}
    for item in items:
        by_name.setdefault(item.name.casefold(), []).append(item.index)
    result: list[int] = []
    for requested in names:
        stripped = requested.strip()
        if stripped.lower().startswith("0x"):
            try:
                program = int(stripped, 16)
            except ValueError as exc:
                raise DependencyError(f"Invalid program number: '{requested}'.") from exc
            index = by_program.get(program)
            if index is None:
                raise DependencyError(f"MOTIF XS item not found: 0x{program:08X}.")
            result.append(index)
            continue
        matches = by_name.get(stripped.casefold(), [])
        if not matches:
            raise DependencyError(f"MOTIF XS item not found: '{requested}'.")
        if len(matches) > 1:
            programs = ", ".join(
                f"0x{items[index].program_number:08X}" for index in matches
            )
            raise DependencyError(
                f"Ambiguous name '{requested}' ({programs}). Use the program number."
            )
        result.append(matches[0])
    return tuple(dict.fromkeys(result))


def _npno_indices_by_names(
    library: NpnoLibrary, names: list[str] | tuple[str, ...]
) -> tuple[int, ...]:
    display_name = library.selectable_items[0].name
    for requested in names:
        value = requested.strip()
        if value not in {"0", "0x00000000"} and value.casefold() not in {
            display_name.casefold(),
            library.name.casefold(),
        }:
            raise DependencyError(
                f"NPNO instrument not found: '{requested}'. Available: '{display_name}'."
            )
    return (0,)


def _kind_plural(library: ParsedLibrary) -> str:
    return "Voices" if library.selectable_kind == "Voice" else "Performances"


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _unreachable() -> NoReturn:
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
