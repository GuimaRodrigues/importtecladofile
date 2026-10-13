"""Independent, tiny YSFC fixture generator used only by tests."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass


@dataclass(frozen=True)
class SyntheticItem:
    program: int
    name: str
    payload: bytes
    title: str = ""
    references: tuple[int, ...] = ()
    timestamp: int = 1


def make_library(
    *,
    family: str = "X8",
    ysfc_version: str | None = None,
    items_by_suffix: dict[str, tuple[SyntheticItem, ...]] | None = None,
    opaque_blocks: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    if items_by_suffix is None:
        items_by_suffix = {
            "PFM": (
                SyntheticItem(
                    0x003F2000,
                    "0:Part A",
                    classic_dpfm(0x00010001, 0x00010002),
                    "Performance A",
                    (0x00010001, 0x00010002),
                    10,
                ),
                SyntheticItem(
                    0x003F2001,
                    "0:Part B",
                    classic_dpfm(0x00010002),
                    "Performance B",
                    (0x00010002,),
                    11,
                ),
                SyntheticItem(
                    0x003F2002,
                    "0:Part C",
                    classic_dpfm(),
                    "Performance C",
                    (),
                    12,
                ),
            ),
            "WFM": (
                SyntheticItem(0x00010001, "0:Wave One", b"waveform-metadata-1", timestamp=3),
                SyntheticItem(0x00010002, "0:Wave Two", b"waveform-metadata-2", timestamp=4),
            ),
            "WIM": (
                SyntheticItem(0x00010001, "0:Wave One", b"sample-one" * 5, timestamp=3),
                SyntheticItem(0x00010002, "0:Wave Two", b"sample-two" * 7, timestamp=4),
            ),
            "ARP": (
                SyntheticItem(0x00010000, "Arp One", b"arpeggio-one", timestamp=5),
                SyntheticItem(0x00010001, "Arp Two", b"arpeggio-two", timestamp=6),
            ),
            "CRV": (
                SyntheticItem(0x00010000, "Curve One", b"curve-one", timestamp=7),
            ),
            "LST": (SyntheticItem(0x00010000, "Live Set", b"not-a-dependency"),),
        }

    entry_blocks: list[tuple[str, bytes]] = []
    data_blocks: list[tuple[str, bytes]] = []
    version_text = ysfc_version or ("4.0.5" if family == "X7" else "5.0.1")
    has_entry_ids = family != "X7" or version_text in {"4.0.3", "4.0.4", "4.0.5"}
    for suffix, items in items_by_suffix.items():
        entry_block, data_block = make_pair(
            suffix, items, include_entry_ids=has_entry_ids
        )
        entry_blocks.append(("E" + suffix, entry_block))
        data_blocks.append(("D" + suffix, data_block))
    raw_blocks = entry_blocks + [(chunk_id, _opaque_block(chunk_id, body)) for chunk_id, body in opaque_blocks]
    raw_blocks += data_blocks

    catalogue_size = len(raw_blocks) * 8
    library_info = b"\xFF" * 80 + b"\x00"
    offset = 64 + catalogue_size + len(library_info)
    catalogue = bytearray()
    block_bytes = bytearray()
    for chunk_id, block in raw_blocks:
        catalogue.extend(chunk_id.encode("ascii") + struct.pack(">I", offset))
        block_bytes.extend(block)
        offset += len(block)

    header = bytearray(b"\xFF" * 64)
    header[0:16] = b"YAMAHA-YSFC" + b"\x00" * 5
    version = version_text.encode("ascii")
    header[16:32] = version + b"\x00" * (16 - len(version))
    struct.pack_into(">I", header, 32, catalogue_size)
    struct.pack_into(">I", header, 48, len(library_info))
    timestamps = [item.timestamp for values in items_by_suffix.values() for item in values]
    struct.pack_into(">I", header, 60, max(timestamps, default=0) + 1)
    return bytes(header + catalogue + library_info + block_bytes)


def make_pair(
    suffix: str,
    items: tuple[SyntheticItem, ...],
    *,
    include_entry_ids: bool = True,
) -> tuple[bytes, bytes]:
    data_payload = bytearray()
    entry_payload = bytearray()
    data_offset = 12
    for item in items:
        data_record = b"Data" + struct.pack(">I", len(item.payload)) + item.payload
        tail = item.name.encode("utf-8") + b"\x00"
        if suffix == "PFM":
            tail += item.title.encode("utf-8") + b"\x00"
            tail += b"".join(struct.pack(">I", reference) for reference in item.references)
        body = (
            struct.pack(">III", len(item.payload), data_offset, item.program)
            + b"\x00" * 6
            + (struct.pack(">I", item.timestamp) if include_entry_ids else b"")
            + tail
        )
        entry_payload.extend(b"Entr" + struct.pack(">I", len(body)) + body)
        data_payload.extend(data_record)
        data_offset += len(data_record)
    entry_id = ("E" + suffix).encode("ascii")
    data_id = ("D" + suffix).encode("ascii")
    return (
        entry_id + struct.pack(">II", len(entry_payload) + 4, len(items)) + entry_payload,
        data_id + struct.pack(">II", len(data_payload) + 4, len(items)) + data_payload,
    )


def make_motif_library(extension: str = ".X0V") -> bytes:
    extension = extension.upper()
    if extension == ".X0V":
        items_by_suffix = {
            "VCE": (
                SyntheticItem(0x003F0805, "12:256:Motif Voice A", b"voice-a"),
                SyntheticItem(0x003F2811, "33:256:Motif Drum A", b"drum-a"),
            ),
            "WFM": (
                SyntheticItem(1, "Wave One", b"wave-metadata-1"),
                SyntheticItem(2, "Wave Two", b"wave-metadata-2"),
            ),
            "WIM": (
                SyntheticItem(1, "Wave One", b"sample-one"),
                SyntheticItem(2, "Wave Two", b"sample-two"),
            ),
        }
        version = "1.0.0"
    elif extension == ".X0A":
        items_by_suffix = {
            "PFM": (
                SyntheticItem(4, "1:Motif Performance A", b"performance-a"),
                SyntheticItem(8, "2:Motif Performance B", b"performance-b"),
            ),
            "VCE": (
                SyntheticItem(0x003F0800, "12:256:Motif Voice A", b"voice-a"),
                SyntheticItem(0x003F0801, "13:256:Motif Voice B", b"voice-b"),
            ),
            "MST": (SyntheticItem(0, "Master", b"master"),),
        }
        version = "1.0.1"
    else:
        raise ValueError(extension)

    entry_blocks: list[tuple[str, bytes]] = []
    data_blocks: list[tuple[str, bytes]] = []
    for suffix, items in items_by_suffix.items():
        entry_block, data_block = make_motif_pair(suffix, items)
        entry_blocks.append(("E" + suffix, entry_block))
        data_blocks.append(("D" + suffix, data_block))
    raw_blocks = entry_blocks + data_blocks
    catalogue_size = len(raw_blocks) * 8
    offset = 64 + catalogue_size
    catalogue = bytearray()
    block_bytes = bytearray()
    for chunk_id, block in raw_blocks:
        catalogue.extend(chunk_id.encode("ascii") + struct.pack(">I", offset))
        block_bytes.extend(block)
        offset += len(block)

    header = bytearray(b"\xFF" * 64)
    header[0:16] = b"YAMAHA-YSFC" + b"\x00" * 5
    encoded_version = version.encode("ascii")
    header[16:32] = encoded_version + b"\x00" * (16 - len(encoded_version))
    struct.pack_into(">I", header, 32, catalogue_size)
    return bytes(header + catalogue + block_bytes)


def make_motif_pair(
    suffix: str, items: tuple[SyntheticItem, ...]
) -> tuple[bytes, bytes]:
    data_payload = bytearray()
    entry_payload = bytearray()
    data_offset = 12
    for item in items:
        data_record = b"Data" + struct.pack(">I", len(item.payload)) + item.payload
        internal_name = f"{item.program:08X}-{suffix}.bin"
        tail = (
            item.name.encode("utf-8")
            + b"\x00"
            + internal_name.encode("ascii")
            + b"\x00"
        )
        body = (
            b"\x00" * 4
            + struct.pack(">I", len(item.payload))
            + b"\x00" * 4
            + struct.pack(">II", data_offset, item.program)
            + b"\x00"
            + tail
        )
        entry_payload.extend(b"Entr" + struct.pack(">I", len(body)) + body)
        data_payload.extend(data_record)
        data_offset += len(data_record)
    entry_id = ("E" + suffix).encode("ascii")
    data_id = ("D" + suffix).encode("ascii")
    return (
        entry_id + struct.pack(">II", len(entry_payload) + 4, len(items)) + entry_payload,
        data_id + struct.pack(">II", len(data_payload) + 4, len(items)) + data_payload,
    )


def classic_dpfm(*waveform_programs: int) -> bytes:
    """Build the minimal classic framing needed for dependency-remap tests."""
    result = bytearray()
    result.extend(struct.pack(">I", 0))  # Common
    result.extend(struct.pack(">IIII", 0, 0, 0, 0))  # Four effect blocks
    part_count = 1 if waveform_programs else 0
    result.extend(struct.pack(">I", part_count))
    if part_count:
        result.extend(struct.pack(">I", 0))  # Empty Part header
    result.extend(struct.pack(">II", 0, 0))  # AD and Digital Input Parts
    if part_count:
        result.extend(struct.pack(">II", 0, len(waveform_programs)))  # AWM2
        for program in waveform_programs:
            bank = program >> 16
            number = program & 0xFFFF
            element = bytes((1, bank)) + struct.pack("<H", number)
            result.extend(struct.pack(">I", len(element)) + element)
    return bytes(result)


def _opaque_block(chunk_id: str, body: bytes) -> bytes:
    return chunk_id.encode("ascii") + struct.pack(">II", len(body) + 4, 0) + body


def make_npno(*, trim_db: int = 0, gain_tenths_db: int = 0) -> bytes:
    """Build one valid mono, one-stroke CBIN/CNSP piano fixture."""
    prefix_size = 0x732
    record_size = 118
    block_size = 1022
    body = bytearray(prefix_size)
    body[0:4] = b"CNSP"
    struct.pack_into(">H", body, 4, 0x464)
    body[0x1C : 0x1C + len(b"Test Piano#Small")] = b"Test Piano#Small"
    body[0x8C:0x10C] = bytes([60] * 128)
    struct.pack_into(">b", body, 0x40C, gain_tenths_db)
    struct.pack_into(">H", body, 0x61C, 0x464)
    struct.pack_into(">H", body, 0x61E, 1)
    struct.pack_into(">H", body, 0x620, 1)
    struct.pack_into(">H", body, 0x622 + 60 * 2, 1)

    directory_end = prefix_size + record_size
    audio_at = ((directory_end + 192 + block_size - 1) // block_size) * block_size - 192
    record = bytearray(record_size)
    struct.pack_into(">I", record, 0, audio_at)
    record[4] = 0  # attack bank
    record[5] = 0  # velocity layer
    struct.pack_into(">I", record, 6, 956)
    struct.pack_into(">H", record, 10, 1)
    struct.pack_into(">h", record, 0x34, trim_db)
    body.extend(record)
    body.extend(b"\x00" * (audio_at - len(body)))
    body.extend(struct.pack(">H", 8) + b"\x00" * (block_size - 2))

    header = bytearray(44)
    header[0:4] = b"CBIN"
    struct.pack_into("<I", header, 4, 1)
    header[8:12] = b"npno"
    struct.pack_into("<I", header, 0x14, 610)
    struct.pack_into("<I", header, 0x18, zlib.crc32(body) & 0xFFFFFFFF)
    return bytes(header + body)
