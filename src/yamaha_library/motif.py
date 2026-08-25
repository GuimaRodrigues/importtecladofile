"""Conservative MOTIF XS (X0A/X0V) selection and subset export."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .errors import DependencyError, ExportError
from .exporter import ExportResult
from .model import Block, FileFamily, ParsedLibrary, Performance
from .parser import YamahaLibraryParser

COPY_BUFFER_SIZE = 4 * 1024 * 1024
NORMAL_VOICE_PAGES = frozenset({0x08, 0x09, 0x0A})
DRUM_VOICE_PAGE = 0x28


@dataclass(frozen=True, slots=True)
class MotifSelection:
    item_indices: tuple[int, ...]
    source_programs: tuple[int, ...]
    program_map: dict[int, int]
    target_programs: tuple[int, ...]
    position_labels: tuple[str, ...]
    item_kind: str
    preserved_counts: dict[str, int]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    conservative: bool

    @property
    def item_count(self) -> int:
        return len(self.item_indices)

    @property
    def can_export(self) -> bool:
        return self.item_count > 0 and not self.blockers


class MotifSubsetResolver:
    def __init__(self, library: ParsedLibrary) -> None:
        if library.family is not FileFamily.X0:
            raise DependencyError("The MOTIF XS resolver accepts only X0A/X0V files.")
        self.library = library

    def resolve(
        self,
        item_indices: list[int] | tuple[int, ...],
        *,
        conservative: bool = True,
    ) -> MotifSelection:
        indices = tuple(dict.fromkeys(item_indices))
        if not indices:
            raise DependencyError(f"Select at least one {self.library.selectable_kind}.")
        items = self.library.selectable_items
        for index in indices:
            if not 0 <= index < len(items):
                raise DependencyError(f"MOTIF XS index does not exist: {index}.")

        selected = tuple(items[index] for index in indices)
        duplicate_programs = [
            program
            for program, count in Counter(item.program_number for item in selected).items()
            if count > 1
        ]
        if duplicate_programs:
            values = ", ".join(f"0x{value:08X}" for value in duplicate_programs)
            raise DependencyError(f"Duplicate MOTIF XS program numbers: {values}.")

        blockers: list[str] = []
        warnings: list[str] = list(self.library.warnings)
        warnings.append(
            "Experimental X0 support is intended for import into MODX/MONTAGE; the output "
            "has not yet been validated as a complete MOTIF XS restore."
        )
        if self.library.input_extension == ".X0V":
            targets, labels = _compact_voice_programs(selected, blockers)
            preserved_counts = _pair_counts(self.library, excluded_suffix="VCE")
            waveform_count = preserved_counts.get("WFM", 0)
            waveform_summary = (
                "The single X0V Waveform"
                if waveform_count == 1
                else f"All {waveform_count} X0V Waveforms"
            )
            warnings.append(
                f"{waveform_summary} will be preserved in full because "
                "the internal MOTIF XS Voice references are opaque."
            )
        else:
            targets = tuple(range(len(selected)))
            labels = tuple(f"Performance {index + 1}" for index in range(len(selected)))
            preserved_counts = _pair_counts(self.library, excluded_suffix="PFM")
            warnings.append(
                "All Voices and other X0A resources will be preserved to retain the "
                "internal references of the selected Performances."
            )

        if not conservative:
            blockers.append(
                "X0A/X0V require conservative mode until the internal MOTIF XS "
                "references are fully interpreted."
            )

        sources = tuple(item.program_number for item in selected)
        return MotifSelection(
            item_indices=indices,
            source_programs=sources,
            program_map=dict(zip(sources, targets, strict=True)),
            target_programs=targets,
            position_labels=labels,
            item_kind=self.library.selectable_kind,
            preserved_counts=preserved_counts,
            warnings=tuple(dict.fromkeys(warnings)),
            blockers=tuple(dict.fromkeys(blockers)),
            conservative=conservative,
        )


class MotifSubsetExporter:
    def __init__(self, library: ParsedLibrary) -> None:
        if library.family is not FileFamily.X0:
            raise ExportError("The MOTIF XS exporter accepts only X0A/X0V files.")
        self.library = library

    def estimate_size(self, selection: MotifSelection) -> int:
        selected_suffix = self._selected_suffix()
        size = 64 + len(self.library.catalogue) * 8
        for block in self.library.blocks:
            if block.suffix != selected_suffix:
                size += block.total_size
            elif block.is_entry_block:
                size += 12 + sum(
                    len(block.entries[index].raw) for index in selection.item_indices
                )
            elif block.is_data_block:
                size += 12 + sum(
                    block.data_records[index].total_size for index in selection.item_indices
                )
            else:
                raise ExportError(f"Unexpected selective chunk: {block.chunk_id}.")
        return size

    def export(
        self,
        selection: MotifSelection,
        output_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> ExportResult:
        if not selection.can_export:
            details = "\n".join(f"- {item}" for item in selection.blockers)
            raise ExportError(f"MOTIF XS export blocked:\n{details}")

        output = Path(output_path).resolve()
        expected_extension = self.library.output_extension
        if output.suffix.upper() != expected_extension:
            raise ExportError(f"The output must preserve the {expected_extension} extension.")
        if self.library.source_path is not None and output == self.library.source_path:
            raise ExportError("The source file must never be overwritten.")
        report_path = output.with_suffix(output.suffix + ".json")
        if not overwrite:
            existing = [str(path) for path in (output, report_path) if path.exists()]
            if existing:
                raise ExportError("Destination already exists: " + ", ".join(existing) + ". Use --force.")
        output.parent.mkdir(parents=True, exist_ok=True)

        output_temp: Path | None = None
        report_temp: Path | None = None
        try:
            output_temp = _temporary_path(output, suffix=expected_extension)
            digest = hashlib.sha256()
            with output_temp.open("wb") as target:
                self._write_file(target, digest, selection)
                target.flush()
                os.fsync(target.fileno())

            output_size = output_temp.stat().st_size
            expected_size = self.estimate_size(selection)
            if output_size != expected_size:
                raise ExportError(
                    f"Internal X0 failure: output is {output_size} bytes; expected {expected_size}."
                )
            self._validate_round_trip(output_temp, selection)

            report = self._build_report(selection, output_size, digest.hexdigest())
            report_temp = _temporary_path(report_path)
            with report_temp.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(report_temp, report_path)
            report_temp = None
            os.replace(output_temp, output)
            output_temp = None
            return ExportResult(output, report_path, output_size, digest.hexdigest(), report)
        except ExportError:
            raise
        except OSError as exc:
            raise ExportError(f"Failed to write MOTIF XS output: {exc}") from exc
        finally:
            for temporary in (output_temp, report_temp):
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _selected_suffix(self) -> str:
        return "VCE" if self.library.input_extension == ".X0V" else "PFM"

    def _write_file(
        self,
        target: BinaryIO,
        digest: object,
        selection: MotifSelection,
    ) -> None:
        selected_suffix = self._selected_suffix()
        catalogue_size = len(self.library.catalogue) * 8
        header = bytearray(self.library.header.raw)
        struct.pack_into(">I", header, 32, catalogue_size)

        encoded_sizes: list[int] = []
        for block in self.library.blocks:
            if block.suffix != selected_suffix:
                encoded_sizes.append(block.total_size)
            elif block.is_entry_block:
                encoded_sizes.append(
                    12
                    + sum(len(block.entries[index].raw) for index in selection.item_indices)
                )
            else:
                encoded_sizes.append(
                    12
                    + sum(
                        block.data_records[index].total_size
                        for index in selection.item_indices
                    )
                )

        _write_hashed(target, digest, bytes(header))
        output_offset = 64 + catalogue_size
        for block, block_size in zip(self.library.blocks, encoded_sizes, strict=True):
            _write_hashed(
                target,
                digest,
                block.chunk_id.encode("ascii") + struct.pack(">I", output_offset),
            )
            output_offset += block_size

        source = self.library.open_source() if self.library.source_path is not None else None
        try:
            for block in self.library.blocks:
                if block.suffix != selected_suffix:
                    self._copy_source(target, digest, source, block.source_offset, block.total_size)
                elif block.is_entry_block:
                    self._write_selected_entries(target, digest, block, selection)
                else:
                    self._write_selected_data(target, digest, source, block, selection)
        finally:
            if source is not None:
                source.close()

    def _write_selected_entries(
        self,
        target: BinaryIO,
        digest: object,
        block: Block,
        selection: MotifSelection,
    ) -> None:
        entries = tuple(block.entries[index] for index in selection.item_indices)
        data_block = self.library.block_map["D" + block.suffix]
        records = tuple(data_block.data_records[index] for index in selection.item_indices)
        payload_size = sum(len(entry.raw) for entry in entries)
        _write_hashed(
            target,
            digest,
            block.chunk_id.encode("ascii") + struct.pack(">II", payload_size + 4, len(entries)),
        )
        data_offset = 12
        for entry, record in zip(entries, records, strict=True):
            rewritten = entry.rewritten(
                item_size=record.payload_size,
                data_offset=data_offset,
                program_number=selection.program_map[entry.program_number],
            )
            _write_hashed(target, digest, rewritten)
            data_offset += record.total_size

    def _write_selected_data(
        self,
        target: BinaryIO,
        digest: object,
        source: BinaryIO | None,
        block: Block,
        selection: MotifSelection,
    ) -> None:
        records = tuple(block.data_records[index] for index in selection.item_indices)
        payload_size = sum(record.total_size for record in records)
        _write_hashed(
            target,
            digest,
            block.chunk_id.encode("ascii") + struct.pack(">II", payload_size + 4, len(records)),
        )
        for record in records:
            self._copy_source(
                target, digest, source, record.source_offset, record.total_size
            )

    def _copy_source(
        self,
        target: BinaryIO,
        digest: object,
        source: BinaryIO | None,
        offset: int,
        size: int,
    ) -> None:
        if self.library.source_bytes is not None:
            _write_hashed(target, digest, self.library.source_bytes[offset : offset + size])
            return
        if source is None:
            raise ExportError("MOTIF XS source is unavailable during copying.")
        source.seek(offset)
        remaining = size
        while remaining:
            chunk = source.read(min(COPY_BUFFER_SIZE, remaining))
            if not chunk:
                raise ExportError("The MOTIF XS source ended during copying.")
            _write_hashed(target, digest, chunk)
            remaining -= len(chunk)

    def _validate_round_trip(self, path: Path, selection: MotifSelection) -> None:
        reopened = YamahaLibraryParser().parse(path)
        expected_items = tuple(
            self.library.selectable_items[index] for index in selection.item_indices
        )
        if [item.name for item in reopened.selectable_items] != [
            item.name for item in expected_items
        ]:
            raise ExportError("X0 validation failed: reopened names differ from the selection.")
        if tuple(item.program_number for item in reopened.selectable_items) != (
            selection.target_programs
        ):
            raise ExportError("X0 validation failed: the new IDs were not persisted.")
        selected_suffix = self._selected_suffix()
        for block in self.library.blocks:
            if block.suffix == selected_suffix:
                continue
            reopened_block = reopened.block_map.get(block.chunk_id)
            if reopened_block is None or reopened_block.count != block.count:
                raise ExportError(
                    f"X0 validation failed to preserve chunk {block.chunk_id}."
                )

    def _build_report(
        self,
        selection: MotifSelection,
        output_size: int,
        sha256: str,
    ) -> dict[str, object]:
        names = [
            self.library.selectable_items[index].name for index in selection.item_indices
        ]
        return {
            "schema_version": 1,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "source": {
                "name": self.library.source_name,
                "size": self.library.source_size,
                "family": "MOTIF XS",
                "ysfc_version": self.library.header.version,
            },
            "output": {
                "extension": self.library.output_extension,
                "size": output_size,
                "sha256": sha256,
            },
            "selection": {
                "kind": selection.item_kind,
                "items": names,
                "program_remap": {
                    f"0x{source:08X}": f"0x{target:08X}"
                    for source, target in selection.program_map.items()
                },
                "preserved_resources": selection.preserved_counts,
            },
            "conservative_mode": selection.conservative,
            "warnings": list(selection.warnings),
            "blockers": list(selection.blockers),
            "hardware_validation": "required_for_experimental_x0_support",
        }


def _compact_voice_programs(
    selected: tuple[Performance, ...], blockers: list[str]
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    normal_index = 0
    drum_index = 0
    targets: list[int] = []
    labels: list[str] = []
    for item in selected:
        program = item.program_number
        page = (program >> 8) & 0xFF
        if page in NORMAL_VOICE_PAGES:
            if normal_index >= 384:
                blockers.append("X0V exceeds the limit of 384 normal Voices.")
                target = program
            else:
                target = 0x003F0800 | ((normal_index // 128) << 8) | (normal_index % 128)
                normal_index += 1
                labels.append(f"Voice User {normal_index}")
                targets.append(target)
                continue
        elif page == DRUM_VOICE_PAGE:
            if drum_index >= 32:
                blockers.append("X0V exceeds the limit of 32 Drum Voices.")
                target = program
            else:
                target = 0x003F2800 | drum_index
                drum_index += 1
                labels.append(f"Drum User {drum_index}")
                targets.append(target)
                continue
        else:
            blockers.append(
                f"Voice 0x{program:08X} belongs to a MOTIF XS bank that cannot be remapped."
            )
            target = program
        targets.append(target)
        labels.append("preserved position")
    return tuple(targets), tuple(labels)


def _pair_counts(library: ParsedLibrary, *, excluded_suffix: str) -> dict[str, int]:
    return {
        block.suffix: len(block.entries)
        for block in library.blocks
        if block.is_entry_block and block.suffix != excluded_suffix
    }


def _write_hashed(target: BinaryIO, digest: object, data: bytes) -> None:
    target.write(data)
    digest.update(data)  # type: ignore[attr-defined]


def _temporary_path(destination: Path, *, suffix: str = ".tmp") -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=suffix, dir=destination.parent
    )
    os.close(descriptor)
    return Path(name)
