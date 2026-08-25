"""Strict, bounds-checked parser for Yamaha YSFC container files."""

from __future__ import annotations

import mmap
from collections.abc import Buffer
from pathlib import Path

from .binary import read_bytes, read_c_string, read_u8, read_u32be
from .errors import FormatError, UnsupportedFormatError
from .model import (
    SUPPORTED_INPUT_EXTENSIONS,
    Block,
    CatalogueEntry,
    DataRecord,
    EntryRecord,
    FileFamily,
    Header,
    LibraryInfo,
    LibrarySlot,
    ParsedLibrary,
)

MAGIC = b"YAMAHA-YSFC"
HEADER_SIZE = 64
KNOWN_VERSIONS = {
    "1.0.0": FileFamily.X0,
    "1.0.1": FileFamily.X0,
    "4.0.1": FileFamily.X7,
    "4.0.2": FileFamily.X7,
    "4.0.3": FileFamily.X7,
    "4.0.4": FileFamily.X7,
    "4.0.5": FileFamily.X7,
    "5.0.1": FileFamily.X8,
}
KNOWN_SUFFIXES = frozenset(
    {
        "PFM",
        "VCE",
        "WFM",
        "WIM",
        "MLT",
        "MST",
        "ARP",
        "MSQ",
        "LST",
        "CRV",
        "MTN",
        "PTN",
        "PAT",
        "SNG",
        "SYS",
        "FVT",
        "PCH",
        "SPG",
        "SOM",
        "SCH",
        "PMT",
        "SMT",
    }
)


class YamahaLibraryParser:
    """Parse metadata without loading large sample payloads into memory."""

    def parse(self, path: str | Path) -> ParsedLibrary:
        source_path = Path(path).resolve()
        extension, family = _extension_and_family(source_path.name)
        try:
            file_size = source_path.stat().st_size
            with source_path.open("rb") as stream:
                if file_size == 0:
                    raise FormatError("Empty file; YSFC header is missing.")
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as view:
                    return self._parse_view(
                        view,
                        source_name=source_path.name,
                        source_size=file_size,
                        source_path=source_path,
                        source_bytes=None,
                        extension=extension,
                        family=family,
                    )
        except OSError as exc:
            raise FormatError(f"Could not read '{source_path}': {exc}") from exc

    def parse_bytes(self, data: bytes, source_name: str = "fixture.X8L") -> ParsedLibrary:
        extension, family = _extension_and_family(source_name)
        return self._parse_view(
            data,
            source_name=source_name,
            source_size=len(data),
            source_path=None,
            source_bytes=data,
            extension=extension,
            family=family,
        )

    def _parse_view(
        self,
        data: Buffer,
        *,
        source_name: str,
        source_size: int,
        source_path: Path | None,
        source_bytes: bytes | None,
        extension: str,
        family: FileFamily,
    ) -> ParsedLibrary:
        if len(data) < HEADER_SIZE:  # type: ignore[arg-type]
            raise FormatError(
                f"Truncated file: {len(data)} bytes; the header requires {HEADER_SIZE}."  # type: ignore[arg-type]
            )

        signature = read_bytes(data, 0, 16, "signature").rstrip(b"\x00")
        if signature != MAGIC:
            raise FormatError("Invalid signature: expected 'YAMAHA-YSFC'.")

        version_raw = read_bytes(data, 16, 16, "version").split(b"\x00", 1)[0]
        try:
            version = version_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FormatError("Version field is not valid ASCII.") from exc
        version_family = KNOWN_VERSIONS.get(version)
        if version_family is None:
            raise UnsupportedFormatError(
                f"YSFC version is not safely supported: '{version or '<empty>'}'. "
                "Known versions: 1.0.0/1.0.1 (X0), 4.0.1–4.0.5 (X7), and "
                "5.0.1 (X8)."
            )
        if version_family is not family:
            raise FormatError(
                f"Extension {extension} indicates {family.value}, but header version {version} "
                f"indicates {version_family.value}."
            )

        catalogue_size = read_u32be(data, 32, "catalogue size")
        declared_library_info_size = read_u32be(data, 48, "Library Info size")
        if family is FileFamily.X0:
            if declared_library_info_size not in {0, 0xFFFFFFFF}:
                raise FormatError(
                    "MOTIF XS header declares an unexpected Library Info area: "
                    f"{declared_library_info_size} bytes."
                )
            library_info_size = 0
        else:
            library_info_size = declared_library_info_size
        next_timestamp = read_u32be(data, 60, "next timestamp")
        header = Header(
            raw=read_bytes(data, 0, HEADER_SIZE, "header"),
            version=version,
            catalogue_size=catalogue_size,
            library_info_size=library_info_size,
            next_timestamp=next_timestamp,
        )

        if catalogue_size % 8:
            raise FormatError(
                f"Inconsistent catalogue: {catalogue_size} bytes is not a multiple of 8."
            )
        catalogue_end = HEADER_SIZE + catalogue_size
        library_end = catalogue_end + library_info_size
        if library_end > source_size:
            raise FormatError(
                "Truncated file: catalogue and Library Info end at "
                f"{library_end}, beyond the file size of {source_size}."
            )

        catalogue = self._parse_catalogue(data, catalogue_size, library_end, source_size)
        if family is FileFamily.X0:
            library_info = LibraryInfo(raw=b"", occupied_slots=())
        else:
            library_raw = read_bytes(data, catalogue_end, library_info_size, "Library Info")
            library_info = self._parse_library_info(library_raw)
        blocks, warnings = self._parse_blocks(
            data, catalogue, library_end, source_size, family, header.has_entry_ids
        )
        self._validate_pairs(blocks)

        return ParsedLibrary(
            source_path=source_path,
            source_bytes=source_bytes,
            source_name=source_name,
            source_size=source_size,
            family=family,
            input_extension=extension,
            header=header,
            catalogue=catalogue,
            library_info=library_info,
            blocks=blocks,
            warnings=warnings,
        )

    def _parse_catalogue(
        self,
        data: Buffer,
        size: int,
        minimum_block_offset: int,
        source_size: int,
    ) -> tuple[CatalogueEntry, ...]:
        result: list[CatalogueEntry] = []
        seen: set[str] = set()
        previous_offset = minimum_block_offset - 1
        for position in range(HEADER_SIZE, HEADER_SIZE + size, 8):
            raw_id = read_bytes(data, position, 4, "chunk ID")
            try:
                chunk_id = raw_id.decode("ascii")
            except UnicodeDecodeError as exc:
                raise FormatError(f"Non-ASCII chunk ID at 0x{position:x}.") from exc
            if len(chunk_id) != 4 or not chunk_id.isprintable():
                raise FormatError(f"Invalid chunk ID at 0x{position:x}: {raw_id!r}.")
            if chunk_id in seen:
                raise FormatError(f"Duplicate chunk ID in catalogue: {chunk_id}.")
            offset = read_u32be(data, position + 4, f"{chunk_id} offset")
            if offset < minimum_block_offset or offset >= source_size:
                raise FormatError(
                    f"{chunk_id} offset is out of bounds: {offset}; "
                    f"blocks must start between {minimum_block_offset} and {source_size - 1}."
                )
            if offset <= previous_offset:
                raise FormatError(
                    f"Catalogue offsets are not strictly increasing at {chunk_id}: {offset}."
                )
            result.append(CatalogueEntry(chunk_id, offset))
            previous_offset = offset
            seen.add(chunk_id)
        return tuple(result)

    def _parse_library_info(self, raw: bytes) -> LibraryInfo:
        if len(raw) < 81:
            raise FormatError(
                f"Truncated Library Info: {len(raw)} bytes; the known minimum is 81."
            )
        occupied_count = read_u8(raw, 80, "occupied slot count")
        if occupied_count > 8:
            raise FormatError(f"Library Info declares {occupied_count} slots; the maximum is 8.")
        position = 81
        slots: list[LibrarySlot] = []
        for index in range(occupied_count):
            slot_number = read_u8(raw, position, f"slot {index + 1}")
            position += 1
            if not 2 <= slot_number <= 9:
                raise FormatError(f"Invalid slot number in Library Info: {slot_number}.")
            name, position = read_c_string(raw, position, len(raw), "library name")
            performance_favourites, position = _read_sized_blob(
                raw, position, "Performance favourites"
            )
            arpeggio_favourites, position = _read_sized_blob(
                raw, position, "Arpeggio favourites"
            )
            waveform_favourites, position = _read_sized_blob(
                raw, position, "Waveform favourites"
            )
            marker = read_u8(raw, position, "Library Info marker")
            position += 1
            if marker != 0xFF:
                raise FormatError(
                    f"Invalid marker in Library Info slot {slot_number}: 0x{marker:02x}."
                )
            non_waveform_size = read_u32be(raw, position, "non-Waveform size")
            waveform_size = read_u32be(raw, position + 4, "Waveform size")
            timestamp = read_u32be(raw, position + 8, "library timestamp")
            position += 12
            terminator = read_u8(raw, position, "Library Info terminator")
            position += 1
            if terminator != 0:
                raise FormatError(
                    f"Invalid terminator in Library Info slot {slot_number}: "
                    f"0x{terminator:02x}."
                )
            slots.append(
                LibrarySlot(
                    slot_number=slot_number,
                    name=name,
                    performance_favourites=performance_favourites,
                    arpeggio_favourites=arpeggio_favourites,
                    waveform_favourites=waveform_favourites,
                    non_waveform_size=non_waveform_size,
                    waveform_size=waveform_size,
                    timestamp=timestamp,
                )
            )
        if position != len(raw):
            raise FormatError(
                f"Library Info contains {len(raw) - position} uninterpreted trailing bytes."
            )
        return LibraryInfo(raw=raw, occupied_slots=tuple(slots))

    def _parse_blocks(
        self,
        data: Buffer,
        catalogue: tuple[CatalogueEntry, ...],
        expected_first_offset: int,
        source_size: int,
        family: FileFamily,
        has_entry_ids: bool,
    ) -> tuple[tuple[Block, ...], tuple[str, ...]]:
        result: list[Block] = []
        warnings: list[str] = []
        previous_end = expected_first_offset
        for catalog_index, catalog_entry in enumerate(catalogue):
            if catalog_entry.offset < previous_end:
                raise FormatError(
                    f"Chunk {catalog_entry.chunk_id} overlaps the previous chunk."
                )
            if catalog_entry.offset > previous_end:
                gap = read_bytes(
                    data,
                    previous_end,
                    catalog_entry.offset - previous_end,
                    "alignment between chunks",
                )
                if any(byte not in (0x00, 0xFF) for byte in gap):
                    raise FormatError(
                        f"There is unrecognized data between offsets {previous_end} and "
                        f"{catalog_entry.offset}."
                    )
                warnings.append(
                    f"The {len(gap)}-byte alignment before {catalog_entry.chunk_id} will be recalculated."
                )
            block = self._parse_block(
                data, catalog_entry, source_size, family, has_entry_ids
            )
            next_offset = (
                catalogue[catalog_index + 1].offset
                if catalog_index + 1 < len(catalogue)
                else source_size
            )
            if block.source_offset + block.total_size > next_offset:
                raise FormatError(f"Chunk {block.chunk_id} extends into the next block or past EOF.")
            result.append(block)
            previous_end = block.source_offset + block.total_size
            if block.suffix not in KNOWN_SUFFIXES:
                warnings.append(
                    f"Chunk {block.chunk_id} is structurally recognized but has unknown semantics."
                )

        if previous_end < source_size:
            tail = read_bytes(data, previous_end, source_size - previous_end, "end of file")
            if any(byte not in (0x00, 0xFF) for byte in tail):
                raise FormatError(
                    f"File contains {len(tail)} uncatalogued bytes after the final chunk."
                )
            warnings.append(f"The final {len(tail)} bytes of padding will be recalculated.")
        return tuple(result), tuple(warnings)

    def _parse_block(
        self,
        data: Buffer,
        item: CatalogueEntry,
        source_size: int,
        family: FileFamily,
        has_entry_ids: bool,
    ) -> Block:
        offset = item.offset
        actual_id_raw = read_bytes(data, offset, 4, f"{item.chunk_id} header")
        try:
            actual_id = actual_id_raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise FormatError(f"Invalid internal ID in {item.chunk_id}.") from exc
        if actual_id != item.chunk_id:
            raise FormatError(
                f"Catalogue points to {item.chunk_id}, but the block at {offset} declares {actual_id!r}."
            )
        remaining_length = read_u32be(data, offset + 4, f"{item.chunk_id} size")
        if remaining_length < 4:
            raise FormatError(f"Invalid {item.chunk_id} chunk size: {remaining_length}.")
        total_size = remaining_length + 8
        block_end = offset + total_size
        if block_end > source_size:
            raise FormatError(
                f"Truncated {item.chunk_id} chunk: ends at {block_end}, "
                f"file ends at {source_size}."
            )
        count = read_u32be(data, offset + 8, f"{item.chunk_id} count")

        if item.chunk_id.startswith("E"):
            entries = self._parse_entries(
                data,
                item.chunk_id,
                offset + 12,
                block_end,
                count,
                family,
                has_entry_ids,
            )
            return Block(item.chunk_id, offset, total_size, count, entries=entries)
        if item.chunk_id.startswith("D"):
            records = self._parse_data_records(data, item.chunk_id, offset + 12, block_end, count)
            return Block(item.chunk_id, offset, total_size, count, data_records=records)
        return Block(item.chunk_id, offset, total_size, count, opaque=True)

    def _parse_entries(
        self,
        data: Buffer,
        chunk_id: str,
        position: int,
        block_end: int,
        count: int,
        family: FileFamily,
        has_entry_ids: bool,
    ) -> tuple[EntryRecord, ...]:
        entries: list[EntryRecord] = []
        for index in range(count):
            if position + 8 > block_end:
                raise FormatError(f"{chunk_id} is truncated before Entr {index + 1}/{count}.")
            if read_bytes(data, position, 4, "magic Entr") != b"Entr":
                raise FormatError(f"Invalid Entr magic in {chunk_id}, offset {position}.")
            body_size = read_u32be(data, position + 4, "Entr size")
            total_size = body_size + 8
            if total_size < 30 or position + total_size > block_end:
                raise FormatError(
                    f"Invalid Entr size in {chunk_id}, offset {position}: {total_size}."
                )
            raw = read_bytes(data, position, total_size, f"Entr in {chunk_id}")
            is_motif = family is FileFamily.X0
            item_size_position = 12 if is_motif else 8
            data_offset_position = 20 if is_motif else 12
            program_number_position = 24 if is_motif else 16
            string_start = 29 if is_motif else (30 if has_entry_ids else 26)
            name, string_position = read_c_string(
                raw, string_start, len(raw), f"name in {chunk_id}"
            )
            title = ""
            reference_positions: tuple[int, ...] = ()
            references: tuple[int, ...] = ()
            if is_motif and string_position < len(raw):
                # MOTIF XS stores a second, internal filename after the display name.
                _, string_position = read_c_string(
                    raw, string_position, len(raw), f"internal name in {chunk_id}"
                )
            elif chunk_id == "EPFM":
                title, string_position = read_c_string(
                    raw, string_position, len(raw), "Performance title"
                )
                remaining = len(raw) - string_position
                if remaining % 4:
                    raise FormatError(
                        f"Misaligned Waveform dependencies in EPFM, offset {position}."
                    )
                reference_positions = tuple(range(string_position, len(raw), 4))
                references = tuple(
                    read_u32be(raw, ref_position, "Waveform reference")
                    for ref_position in reference_positions
                )
            entries.append(
                EntryRecord(
                    raw=raw,
                    source_offset=position,
                    item_size=read_u32be(
                        raw, item_size_position, "item size in Entr"
                    ),
                    data_offset=read_u32be(
                        raw, data_offset_position, "item offset in Entr"
                    ),
                    program_number=read_u32be(
                        raw, program_number_position, "program number"
                    ),
                    flags=(
                        read_bytes(raw, 28, 1, "reservado Entr MOTIF")
                        if is_motif
                        else read_bytes(raw, 20, 6, "flags Entr")
                    ),
                    timestamp=(
                        read_u32be(raw, 26, "Entr sort ID")
                        if has_entry_ids
                        else 0
                    ),
                    name=name,
                    title=title,
                    reference_positions=reference_positions,
                    references=references,
                    item_size_position=item_size_position,
                    data_offset_position=data_offset_position,
                    program_number_position=program_number_position,
                )
            )
            position += total_size
        if position != block_end:
            raise FormatError(
                f"Inconsistent count/size in {chunk_id}: "
                f"{block_end - position} bytes remain after {count} Entr records."
            )
        return tuple(entries)

    def _parse_data_records(
        self,
        data: Buffer,
        chunk_id: str,
        position: int,
        block_end: int,
        count: int,
    ) -> tuple[DataRecord, ...]:
        records: list[DataRecord] = []
        for index in range(count):
            if position + 8 > block_end:
                raise FormatError(f"{chunk_id} is truncated before Data {index + 1}/{count}.")
            if read_bytes(data, position, 4, "magic Data") != b"Data":
                raise FormatError(f"Invalid Data magic in {chunk_id}, offset {position}.")
            payload_size = read_u32be(data, position + 4, "Data size")
            total_size = payload_size + 8
            if position + total_size > block_end:
                raise FormatError(
                    f"Truncated Data in {chunk_id}, offset {position}: "
                    f"{payload_size} bytes declared."
                )
            records.append(DataRecord(source_offset=position, payload_size=payload_size))
            position += total_size
        if position != block_end:
            raise FormatError(
                f"Inconsistent count/size in {chunk_id}: "
                f"{block_end - position} bytes remain after {count} Data records."
            )
        return tuple(records)

    def _validate_pairs(self, blocks: tuple[Block, ...]) -> None:
        block_map = {block.chunk_id: block for block in blocks}
        for entry_block in (block for block in blocks if block.is_entry_block):
            data_id = "D" + entry_block.suffix
            data_block = block_map.get(data_id)
            if data_block is None:
                raise FormatError(f"Block {entry_block.chunk_id} has no matching {data_id} block.")
            if data_block.opaque or len(entry_block.entries) != len(data_block.data_records):
                raise FormatError(
                    f"Pair {entry_block.chunk_id}/{data_id} has different counts: "
                    f"{len(entry_block.entries)} and {len(data_block.data_records)}."
                )
            for index, (entry, record) in enumerate(
                zip(entry_block.entries, data_block.data_records, strict=True)
            ):
                actual_offset = record.source_offset - data_block.source_offset
                if entry.item_size != record.payload_size:
                    raise FormatError(
                        f"Item {index} in {entry_block.chunk_id}/{data_id}: Entr declares "
                        f"{entry.item_size} bytes, Data declares {record.payload_size}."
                    )
                if entry.data_offset != actual_offset:
                    raise FormatError(
                        f"Item {index} in {entry_block.chunk_id}: offset {entry.data_offset}, "
                        f"expected {actual_offset}."
                    )
        for data_block in (block for block in blocks if block.is_data_block):
            entry_id = "E" + data_block.suffix
            if entry_id not in block_map:
                raise FormatError(f"Block {data_block.chunk_id} has no matching {entry_id} block.")


def _extension_and_family(filename: str) -> tuple[str, FileFamily]:
    extension = Path(filename).suffix.upper()
    if extension not in SUPPORTED_INPUT_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_INPUT_EXTENSIONS))
        raise UnsupportedFormatError(
            f"Unsupported extension '{Path(filename).suffix or '<missing>'}'. Use: {supported}."
        )
    if extension.startswith(".X0"):
        family = FileFamily.X0
    elif extension.startswith(".X7"):
        family = FileFamily.X7
    else:
        family = FileFamily.X8
    return extension, family


def _read_sized_blob(raw: bytes, position: int, context: str) -> tuple[bytes, int]:
    size = read_u32be(raw, position, f"{context} size")
    position += 4
    value = read_bytes(raw, position, size, context)
    return value, position + size
