"""Immutable domain model for parsed YSFC files."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


class FileFamily(StrEnum):
    X0 = "X0"
    X7 = "X7"
    X8 = "X8"

    @property
    def library_extension(self) -> str:
        if self is FileFamily.X0:
            raise ValueError("X0A/X0V preserve the source extension.")
        return f".{self.value}L"

    @property
    def latest_library_version(self) -> str:
        if self is FileFamily.X7:
            return "4.0.5"
        if self is FileFamily.X8:
            return "5.0.1"
        raise ValueError("X0A/X0V do not use X7/X8 Library versions.")


SUPPORTED_INPUT_EXTENSIONS = frozenset(
    {".X0A", ".X0V", ".X7A", ".X7U", ".X7L", ".X8A", ".X8U", ".X8L"}
)


@dataclass(frozen=True, slots=True)
class Header:
    raw: bytes
    version: str
    catalogue_size: int
    library_info_size: int
    next_timestamp: int

    @property
    def has_entry_ids(self) -> bool:
        return ysfc_version_has_entry_ids(self.version)


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    chunk_id: str
    offset: int


@dataclass(frozen=True, slots=True)
class LibrarySlot:
    slot_number: int
    name: str
    performance_favourites: bytes
    arpeggio_favourites: bytes
    waveform_favourites: bytes
    non_waveform_size: int
    waveform_size: int
    timestamp: int


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    raw: bytes
    occupied_slots: tuple[LibrarySlot, ...]


@dataclass(frozen=True, slots=True)
class EntryRecord:
    """A small Entr record copied into memory so it can be safely rewritten."""

    raw: bytes
    source_offset: int
    item_size: int
    data_offset: int
    program_number: int
    flags: bytes
    timestamp: int
    name: str
    title: str
    reference_positions: tuple[int, ...] = ()
    references: tuple[int, ...] = ()
    item_size_position: int = 8
    data_offset_position: int = 12
    program_number_position: int = 16

    @property
    def display_name(self) -> str:
        return self.title or _strip_category(self.name)

    def rewritten(
        self,
        *,
        item_size: int | None = None,
        data_offset: int | None = None,
        program_number: int | None = None,
        reference_map: dict[int, int] | None = None,
    ) -> bytes:
        result = bytearray(self.raw)
        if item_size is not None:
            struct.pack_into(">I", result, self.item_size_position, item_size)
        if data_offset is not None:
            struct.pack_into(">I", result, self.data_offset_position, data_offset)
        if program_number is not None:
            struct.pack_into(">I", result, self.program_number_position, program_number)
        if reference_map:
            for position, old_value in zip(self.reference_positions, self.references, strict=True):
                new_value = reference_map.get(old_value, old_value)
                struct.pack_into(">I", result, position, new_value)
        return bytes(result)

    def remapped(
        self,
        *,
        program_number: int | None = None,
        reference_map: dict[int, int] | None = None,
    ) -> EntryRecord:
        raw = self.rewritten(program_number=program_number, reference_map=reference_map)
        references = tuple((reference_map or {}).get(value, value) for value in self.references)
        return replace(
            self,
            raw=raw,
            program_number=self.program_number if program_number is None else program_number,
            references=references,
        )


@dataclass(frozen=True, slots=True)
class DataRecord:
    source_offset: int
    payload_size: int

    @property
    def total_size(self) -> int:
        return self.payload_size + 8


@dataclass(frozen=True, slots=True)
class Block:
    chunk_id: str
    source_offset: int
    total_size: int
    count: int
    entries: tuple[EntryRecord, ...] = ()
    data_records: tuple[DataRecord, ...] = ()
    opaque: bool = False

    @property
    def suffix(self) -> str:
        return self.chunk_id[1:] if len(self.chunk_id) == 4 else self.chunk_id

    @property
    def is_entry_block(self) -> bool:
        return self.chunk_id.startswith("E") and not self.opaque

    @property
    def is_data_block(self) -> bool:
        return self.chunk_id.startswith("D") and not self.opaque


@dataclass(frozen=True, slots=True)
class Performance:
    index: int
    entry: EntryRecord

    @property
    def name(self) -> str:
        return self.entry.display_name

    @property
    def program_number(self) -> int:
        return self.entry.program_number


@dataclass(frozen=True, slots=True)
class ParsedLibrary:
    source_path: Path | None
    source_bytes: bytes | None
    source_name: str
    source_size: int
    family: FileFamily
    input_extension: str
    header: Header
    catalogue: tuple[CatalogueEntry, ...]
    library_info: LibraryInfo
    blocks: tuple[Block, ...]
    warnings: tuple[str, ...] = ()

    @property
    def block_map(self) -> dict[str, Block]:
        return {block.chunk_id: block for block in self.blocks}

    @property
    def performances(self) -> tuple[Performance, ...]:
        block = self.block_map.get("EPFM")
        if block is None:
            return ()
        return tuple(Performance(index, entry) for index, entry in enumerate(block.entries))

    @property
    def voices(self) -> tuple[Performance, ...]:
        block = self.block_map.get("EVCE")
        if block is None:
            return ()
        return tuple(Performance(index, entry) for index, entry in enumerate(block.entries))

    @property
    def selectable_items(self) -> tuple[Performance, ...]:
        if self.family is FileFamily.X0 and self.input_extension == ".X0V":
            return self.voices
        return self.performances

    @property
    def selectable_kind(self) -> str:
        return "Voice" if self.family is FileFamily.X0 and self.input_extension == ".X0V" else "Performance"

    @property
    def output_extension(self) -> str:
        if self.family is FileFamily.X0:
            return self.input_extension
        return self.family.library_extension

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(item.chunk_id for item in self.catalogue)

    def open_source(self) -> BinaryIO:
        if self.source_path is None:
            raise RuntimeError("This library was created in memory.")
        return self.source_path.open("rb")

    def source_range(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.source_size:
            raise ValueError(f"Range outside the source: {offset}:{offset + size}")
        if self.source_bytes is not None:
            return self.source_bytes[offset : offset + size]
        with self.open_source() as stream:
            stream.seek(offset)
            data = stream.read(size)
        if len(data) != size:
            raise OSError(f"Could not reread {size} bytes at position {offset}.")
        return data


def _strip_category(value: str) -> str:
    parts = value.split(":")
    while len(parts) > 1 and parts[0].isdecimal():
        parts.pop(0)
    return ":".join(parts).rstrip()


def ysfc_version_has_entry_ids(version: str) -> bool:
    try:
        major, minor, patch = (int(value) for value in version.split("."))
    except (TypeError, ValueError):
        return False
    return major >= 5 or (major == 4 and minor == 0 and patch >= 3)
