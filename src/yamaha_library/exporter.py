"""Streaming, atomic writer for conservative YSFC subsets."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .classic_dpfm import remap_classic_waveform_references
from .dependencies import DependencyResolver, DependencySelection
from .errors import ExportError
from .model import Block, EntryRecord, FileFamily, ParsedLibrary, ysfc_version_has_entry_ids
from .parser import YamahaLibraryParser

EMPTY_LIBRARY_INFO = b"\xFF" * 80 + b"\x00"
COPY_BUFFER_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_path: Path
    report_path: Path
    output_size: int
    sha256: str
    report: dict[str, object]


@dataclass(frozen=True, slots=True)
class _PairPlan:
    suffix: str
    entry_block: Block
    data_block: Block
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _RawPlan:
    block: Block


_Plan = _PairPlan | _RawPlan


class LibraryExporter:
    def __init__(
        self,
        library: ParsedLibrary,
        *,
        target_family: FileFamily | None = None,
    ) -> None:
        if library.family not in {FileFamily.X7, FileFamily.X8}:
            raise ExportError("The X7/X8 exporter does not accept MOTIF XS files.")
        if target_family is FileFamily.X0:
            raise ExportError("The target format must be X7L or X8L.")
        self.library = library
        self.target_family = target_family or library.family

    @property
    def output_extension(self) -> str:
        return self.target_family.library_extension

    @property
    def output_version(self) -> str:
        if self.target_family is self.library.family:
            return self.library.header.version
        return self.target_family.latest_library_version

    @property
    def converts_family(self) -> bool:
        return self.target_family is not self.library.family

    @property
    def _target_has_entry_ids(self) -> bool:
        return ysfc_version_has_entry_ids(self.output_version)

    @property
    def _upgrades_legacy_entries(self) -> bool:
        return self._target_has_entry_ids and not self.library.header.has_entry_ids

    def estimate_size(self, selection: DependencySelection) -> int:
        plans = self._plans(selection)
        catalogue_size = sum(16 if isinstance(plan, _PairPlan) else 8 for plan in plans)
        size = 64 + catalogue_size + len(EMPTY_LIBRARY_INFO)
        for plan in plans:
            if isinstance(plan, _RawPlan):
                size += plan.block.total_size
            else:
                entries = tuple(plan.entry_block.entries[index] for index in plan.indices)
                records = tuple(plan.data_block.data_records[index] for index in plan.indices)
                size += 12 + sum(self._encoded_entry_size(entry) for entry in entries)
                size += 12 + sum(record.total_size for record in records)
        return size

    def export(
        self,
        selection: DependencySelection,
        output_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> ExportResult:
        if not selection.can_export:
            details = "\n".join(f"- {item}" for item in selection.blockers)
            raise ExportError(f"Export blocked to prevent an incomplete file:\n{details}")

        output = Path(output_path).resolve()
        expected_extension = self.output_extension
        if output.suffix.upper() != expected_extension:
            raise ExportError(
                f"{self.target_family.value} family output must use {expected_extension}."
            )
        if self.library.source_path is not None and output == self.library.source_path:
            raise ExportError("The source file must never be overwritten.")
        report_path = output.with_suffix(output.suffix + ".json")
        if not overwrite:
            existing = [str(path) for path in (output, report_path) if path.exists()]
            if existing:
                raise ExportError("Destination already exists: " + ", ".join(existing) + ". Use --force.")
        output.parent.mkdir(parents=True, exist_ok=True)

        plans = self._plans(selection)
        output_temp: Path | None = None
        report_temp: Path | None = None
        try:
            output_temp = _temporary_path(output, suffix=expected_extension)
            digest = hashlib.sha256()
            with output_temp.open("wb") as target:
                writer = _HashingWriter(target, digest)
                self._write_file(writer, plans, selection)
                target.flush()
                os.fsync(target.fileno())
            output_size = output_temp.stat().st_size
            expected_size = self.estimate_size(selection)
            if output_size != expected_size:
                raise ExportError(
                    f"Internal failure: output is {output_size} bytes; expected {expected_size}."
                )

            # Reopen the staged result before it can replace any user-visible path.
            reparsed = YamahaLibraryParser().parse(output_temp)
            reopened_names = [item.name for item in reparsed.performances]
            expected_names = [
                self.library.performances[index].name for index in selection.performance_indices
            ]
            if reopened_names != expected_names:
                raise ExportError(
                    "Round-trip validation failed: the reopened Performance list differs from the selection."
                )
            reopened_selection = DependencyResolver(reparsed).resolve(
                tuple(range(len(reparsed.performances))), conservative=selection.conservative
            )
            if not reopened_selection.can_export:
                details = "; ".join(reopened_selection.blockers)
                raise ExportError(
                    "Dependency validation of the remapped output failed: " + details
                )

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
            raise ExportError(f"Failed to write atomic output: {exc}") from exc
        finally:
            for temporary in (output_temp, report_temp):
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _plans(self, selection: DependencySelection) -> tuple[_Plan, ...]:
        block_map = self.library.block_map
        pair_indices: dict[str, tuple[int, ...]] = {
            "PFM": selection.performance_indices,
            "WFM": selection.waveform_indices,
            "WIM": selection.waveform_image_indices,
        }
        for suffix in selection.preserved_suffixes:
            entry = block_map.get("E" + suffix)
            if entry is not None:
                pair_indices[suffix] = tuple(range(len(entry.entries)))

        plans: list[_Plan] = []
        emitted_pairs: set[str] = set()
        for block in self.library.blocks:
            suffix = block.suffix
            if suffix in pair_indices and suffix not in emitted_pairs:
                entry_block = block_map.get("E" + suffix)
                data_block = block_map.get("D" + suffix)
                if entry_block is None or data_block is None:
                    raise ExportError(f"Missing E/D pair while planning {suffix}.")
                plans.append(_PairPlan(suffix, entry_block, data_block, pair_indices[suffix]))
                emitted_pairs.add(suffix)
            elif block.chunk_id in selection.preserved_block_ids and block.opaque:
                plans.append(_RawPlan(block))
        return tuple(plans)

    def _write_file(
        self,
        target: _HashingWriter,
        plans: tuple[_Plan, ...],
        selection: DependencySelection,
    ) -> None:
        chunk_count = sum(2 if isinstance(plan, _PairPlan) else 1 for plan in plans)
        catalogue_size = chunk_count * 8
        header = bytearray(self.library.header.raw)
        encoded_version = self.output_version.encode("ascii")
        header[16:32] = encoded_version + b"\x00" * (16 - len(encoded_version))
        struct.pack_into(">I", header, 32, catalogue_size)
        struct.pack_into(">I", header, 48, len(EMPTY_LIBRARY_INFO))

        entry_timestamps = self._entry_timestamps(plans)
        if self._target_has_entry_ids:
            selected_timestamps = list(entry_timestamps.values())
            next_timestamp = max(selected_timestamps, default=0) + 1
            struct.pack_into(">I", header, 60, min(next_timestamp, 0xFFFFFFFF))

        encoded_blocks: list[tuple[str, int, _Plan, bool]] = []
        output_offset = 64 + catalogue_size + len(EMPTY_LIBRARY_INFO)
        for plan in plans:
            if isinstance(plan, _RawPlan):
                encoded_blocks.append((plan.block.chunk_id, output_offset, plan, False))
                output_offset += plan.block.total_size
                continue
            entry_size = 12 + sum(
                self._encoded_entry_size(plan.entry_block.entries[index])
                for index in plan.indices
            )
            data_size = 12 + sum(
                plan.data_block.data_records[index].total_size for index in plan.indices
            )
            encoded_blocks.append((plan.entry_block.chunk_id, output_offset, plan, False))
            output_offset += entry_size
            encoded_blocks.append((plan.data_block.chunk_id, output_offset, plan, True))
            output_offset += data_size

        target.write(bytes(header))
        for chunk_id, offset, _, _ in encoded_blocks:
            target.write(chunk_id.encode("ascii") + struct.pack(">I", offset))
        target.write(EMPTY_LIBRARY_INFO)

        source = self.library.open_source() if self.library.source_path is not None else None
        try:
            for _, _, plan, is_data in encoded_blocks:
                if isinstance(plan, _RawPlan):
                    self._copy_source(target, source, plan.block.source_offset, plan.block.total_size)
                elif is_data:
                    self._write_data_block(target, source, plan, selection)
                else:
                    self._write_entry_block(target, plan, selection, entry_timestamps)
        finally:
            if source is not None:
                source.close()

    def _write_entry_block(
        self,
        target: _HashingWriter,
        plan: _PairPlan,
        selection: DependencySelection,
        entry_timestamps: dict[int, int],
    ) -> None:
        entries = tuple(plan.entry_block.entries[index] for index in plan.indices)
        records = tuple(plan.data_block.data_records[index] for index in plan.indices)
        payload_size = sum(self._encoded_entry_size(entry) for entry in entries)
        target.write(plan.entry_block.chunk_id.encode("ascii"))
        target.write(struct.pack(">II", payload_size + 4, len(entries)))
        data_offset = 12
        for entry, record in zip(entries, records, strict=True):
            program_number: int | None = None
            reference_map: dict[int, int] | None = None
            if plan.suffix == "PFM":
                program_number = selection.performance_program_map[entry.program_number]
                reference_map = selection.waveform_program_map
            elif plan.suffix in {"WFM", "WIM"}:
                program_number = selection.waveform_program_map[entry.program_number]
            encoded_entry = entry.rewritten(
                item_size=record.payload_size,
                data_offset=data_offset,
                program_number=program_number,
                reference_map=reference_map,
            )
            if self._upgrades_legacy_entries:
                encoded_entry = _add_entry_timestamp(
                    encoded_entry, entry_timestamps[entry.source_offset]
                )
            target.write(encoded_entry)
            data_offset += record.total_size

    def _encoded_entry_size(self, entry: EntryRecord) -> int:
        raw_size = len(entry.raw)
        return raw_size + (4 if self._upgrades_legacy_entries else 0)

    def _entry_timestamps(self, plans: tuple[_Plan, ...]) -> dict[int, int]:
        result: dict[int, int] = {}
        next_timestamp = 1
        for plan in plans:
            if not isinstance(plan, _PairPlan):
                continue
            for index in plan.indices:
                entry = plan.entry_block.entries[index]
                if self.library.header.has_entry_ids:
                    timestamp = entry.timestamp
                else:
                    timestamp = next_timestamp
                    next_timestamp += 1
                result[entry.source_offset] = timestamp
        return result

    def _write_data_block(
        self,
        target: _HashingWriter,
        source: BinaryIO | None,
        plan: _PairPlan,
        selection: DependencySelection,
    ) -> None:
        records = tuple(plan.data_block.data_records[index] for index in plan.indices)
        payload_size = sum(record.total_size for record in records)
        target.write(plan.data_block.chunk_id.encode("ascii"))
        target.write(struct.pack(">II", payload_size + 4, len(records)))
        for record in records:
            if plan.suffix == "PFM":
                payload = self._read_source(
                    source, record.source_offset + 8, record.payload_size
                )
                rewritten = remap_classic_waveform_references(
                    payload, selection.waveform_program_map
                )
                if len(rewritten) != record.payload_size:
                    raise ExportError("DPFM remapping unexpectedly changed the item size.")
                target.write(b"Data" + struct.pack(">I", len(rewritten)) + rewritten)
            else:
                self._copy_source(target, source, record.source_offset, record.total_size)

    def _read_source(self, source: BinaryIO | None, offset: int, size: int) -> bytes:
        if self.library.source_bytes is not None:
            return self.library.source_bytes[offset : offset + size]
        if source is None:
            raise ExportError("Source is unavailable during data remapping.")
        source.seek(offset)
        data = source.read(size)
        if len(data) != size:
            raise ExportError(
                f"Source ended while reading at {offset}: "
                f"expected {size}, got {len(data)}."
            )
        return data

    def _copy_source(
        self,
        target: _HashingWriter,
        source: BinaryIO | None,
        offset: int,
        size: int,
    ) -> None:
        if self.library.source_bytes is not None:
            target.write(self.library.source_bytes[offset : offset + size])
            return
        if source is None:
            raise ExportError("Source is unavailable during data copying.")
        source.seek(offset)
        remaining = size
        while remaining:
            chunk = source.read(min(COPY_BUFFER_SIZE, remaining))
            if not chunk:
                raise ExportError(
                    f"Source ended during copying at {offset + size - remaining}."
                )
            target.write(chunk)
            remaining -= len(chunk)

    def _build_report(
        self,
        selection: DependencySelection,
        output_size: int,
        sha256: str,
    ) -> dict[str, object]:
        performance_names = [
            self.library.performances[index].name for index in selection.performance_indices
        ]
        return {
            "schema_version": 1,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "source": {
                "name": self.library.source_name,
                "size": self.library.source_size,
                "family": self.library.family.value,
                "ysfc_version": self.library.header.version,
            },
            "output": {
                "family": self.target_family.value,
                "ysfc_version": self.output_version,
                "extension": self.output_extension,
                "converted_from_source_family": self.converts_family,
                "size": output_size,
                "sha256": sha256,
            },
            "selection": {
                "performances": performance_names,
                "performance_programs": [
                    f"0x{value:08X}" for value in selection.performance_programs
                ],
                "performance_program_remap": {
                    f"0x{source:08X}": f"0x{target:08X}"
                    for source, target in selection.performance_program_map.items()
                },
                "waveform_programs": [
                    f"0x{value:08X}" for value in selection.waveform_programs
                ],
                "waveform_count": selection.waveform_count,
                "waveform_program_remap": {
                    f"0x{source:08X}": f"0x{target:08X}"
                    for source, target in selection.waveform_program_map.items()
                },
                "waveform_image_sample_payload_count": selection.sample_count,
                "shared_waveforms": {
                    f"0x{key:08X}": value for key, value in selection.shared_waveforms.items()
                },
                "conservatively_preserved_resources": selection.resource_counts,
            },
            "conservative_mode": selection.conservative,
            "warnings": list(selection.warnings),
            "blockers": list(selection.blockers),
            "hardware_validation": "not_performed",
        }


class _HashingWriter:
    def __init__(self, stream: BinaryIO, digest: object) -> None:
        self.stream = stream
        self.digest = digest

    def write(self, data: bytes) -> None:
        self.stream.write(data)
        self.digest.update(data)  # type: ignore[attr-defined]


def _add_entry_timestamp(raw: bytes, timestamp: int) -> bytes:
    if len(raw) < 26 or raw[:4] != b"Entr":
        raise ExportError("Invalid legacy Entr record during X7/X8 conversion.")
    body_size = struct.unpack_from(">I", raw, 4)[0]
    result = bytearray(raw[:26] + b"\x00\x00\x00\x00" + raw[26:])
    struct.pack_into(">I", result, 4, body_size + 4)
    struct.pack_into(">I", result, 26, timestamp)
    return bytes(result)


def _temporary_path(destination: Path, *, suffix: str = ".tmp") -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=suffix, dir=destination.parent
    )
    os.close(descriptor)
    return Path(name)
