# Portions of the CNSP parser/decoder are derived from nord-format.
# Copyright (c) 2023-2026 John Moore. See LICENSES/BSD-3-Clause-nord-format.txt.
"""Reader and audio decoder for Nord Piano Library (``.npno``) files.

The format is a little-endian CBIN container around a big-endian CNSP stream.
Only immutable metadata is retained; encoded audio remains in the source file and
is decoded one stroke at a time during conversion.
"""

from __future__ import annotations

import mmap
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import FormatError, UnsupportedFormatError

CBIN_HEADER_SIZE = 0x2C
CNSP_PREFIX_SIZE = 0x732
STROKE_RECORD_SIZE = 118
AUDIO_ALIGN_BIAS = 192
BLOCK_WORDS = 511
BLOCK_OVERLAP = 64
SAMPLE_RATE = 35_002
SUPPORTED_STREAM_VERSIONS = frozenset({0x450, 0x464})


@dataclass(frozen=True, slots=True)
class NpnoHeader:
    container_version: int
    stream_version: int
    stored_crc32: int
    checksum_ok: bool

    @property
    def version(self) -> str:
        return f"{self.container_version / 100:.2f} / CNSP 0x{self.stream_version:03X}"


@dataclass(frozen=True, slots=True)
class NpnoStroke:
    index: int
    root: int
    bank: int
    layer: int
    frames: int
    blocks: int
    seeds: tuple[tuple[int, int, int, int], tuple[int, int, int, int]]
    trim_tenths_db: int
    audio_offset: int
    audio_size: int


@dataclass(frozen=True, slots=True)
class NpnoInstrument:
    index: int
    name: str
    program_number: int = 0


@dataclass(frozen=True, slots=True)
class NpnoLibrary:
    source_path: Path
    source_name: str
    source_size: int
    header: NpnoHeader
    name: str
    variant: str
    channels: int
    key_map: tuple[int, ...]
    fine_tune: tuple[int, ...]
    gain_tenths_db: int
    damper_top: int
    strokes: tuple[NpnoStroke, ...]
    warnings: tuple[str, ...]

    @property
    def input_extension(self) -> str:
        return ".NPNO"

    @property
    def output_extension(self) -> str:
        return ".X8L"

    @property
    def selectable_items(self) -> tuple[NpnoInstrument, ...]:
        display = self.name if not self.variant else f"{self.name} ({self.variant})"
        return (NpnoInstrument(0, display),)

    @property
    def selectable_kind(self) -> str:
        return "Instrument"

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return ("CNSP",)

    @property
    def attack_strokes(self) -> tuple[NpnoStroke, ...]:
        return tuple(stroke for stroke in self.strokes if stroke.bank == 0)

    @property
    def resonance_strokes(self) -> tuple[NpnoStroke, ...]:
        return tuple(stroke for stroke in self.strokes if stroke.bank == 1)

    @property
    def release_strokes(self) -> tuple[NpnoStroke, ...]:
        return tuple(stroke for stroke in self.strokes if stroke.bank == 2)


class NpnoParser:
    """Parse and validate a Nord CBIN/CNSP piano library."""

    def parse(self, path: str | Path) -> NpnoLibrary:
        source_path = Path(path).resolve()
        if source_path.suffix.casefold() != ".npno":
            raise UnsupportedFormatError("Nord Piano input must use the .npno extension.")
        try:
            source_size = source_path.stat().st_size
            with source_path.open("rb") as stream:
                if source_size < CBIN_HEADER_SIZE + CNSP_PREFIX_SIZE:
                    raise FormatError("Truncated NPNO file: the CNSP prefix is incomplete.")
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as view:
                    parsed = self._parse_view(view, source_path, source_size)
            computed_crc = _crc32_body(source_path)
        except OSError as exc:
            raise FormatError(f"Could not read '{source_path}': {exc}") from exc
        if computed_crc != parsed.header.stored_crc32:
            raise FormatError(
                "Invalid NPNO checksum: stored "
                f"0x{parsed.header.stored_crc32:08X}, computed 0x{computed_crc:08X}."
            )
        return NpnoLibrary(
            source_path=parsed.source_path,
            source_name=parsed.source_name,
            source_size=parsed.source_size,
            header=NpnoHeader(
                parsed.header.container_version,
                parsed.header.stream_version,
                parsed.header.stored_crc32,
                True,
            ),
            name=parsed.name,
            variant=parsed.variant,
            channels=parsed.channels,
            key_map=parsed.key_map,
            fine_tune=parsed.fine_tune,
            gain_tenths_db=parsed.gain_tenths_db,
            damper_top=parsed.damper_top,
            strokes=parsed.strokes,
            warnings=parsed.warnings,
        )

    def _parse_view(
        self, data: mmap.mmap, source_path: Path, source_size: int
    ) -> NpnoLibrary:
        if data[0:4] != b"CBIN":
            raise FormatError("Invalid NPNO signature: expected 'CBIN'.")
        generation = struct.unpack_from("<I", data, 4)[0]
        if generation != 1:
            raise UnsupportedFormatError(
                f"Unsupported NPNO CBIN generation {generation}; expected generation 1."
            )
        if data[8:12] != b"npno":
            tag = bytes(data[8:12]).decode("ascii", errors="replace")
            raise FormatError(f"CBIN tag is '{tag}', not 'npno'.")
        if any(data[0x1C:CBIN_HEADER_SIZE]):
            raise FormatError("Invalid NPNO header: the CBIN padding is not zero.")

        container_version = struct.unpack_from("<I", data, 0x14)[0]
        stored_crc = struct.unpack_from("<I", data, 0x18)[0]
        body = memoryview(data)[CBIN_HEADER_SIZE:]
        try:
            if bytes(body[0:4]) != b"CNSP":
                raise FormatError("Invalid NPNO body: expected the 'CNSP' stream.")
            stream_version = _u16be(body, 4)
            if stream_version not in SUPPORTED_STREAM_VERSIONS:
                known = ", ".join(f"0x{value:03X}" for value in sorted(SUPPORTED_STREAM_VERSIONS))
                raise UnsupportedFormatError(
                    f"Unsupported CNSP stream version 0x{stream_version:03X}; known: {known}."
                )
            if _u16be(body, 0x61C) != stream_version:
                raise FormatError("The CNSP stream-version echo does not match the header.")
            channels = _u16be(body, 0x61E)
            if channels not in {1, 2}:
                raise FormatError(f"NPNO declares {channels} channels; expected 1 or 2.")

            count = _u16be(body, 0x620)
            root_counts = tuple(_u16be(body, 0x622 + note * 2) for note in range(128))
            if sum(root_counts) != count:
                raise FormatError(
                    f"NPNO root counts total {sum(root_counts)}, not the declared {count}."
                )
            directory_end = CNSP_PREFIX_SIZE + count * STROKE_RECORD_SIZE
            if directory_end > len(body):
                raise FormatError("Truncated NPNO stroke directory.")
            block_size = BLOCK_WORDS * 2 * channels
            audio_at = _first_audio_offset(directory_end, block_size)
            if audio_at > len(body) or any(body[directory_end:audio_at]):
                raise FormatError("Invalid NPNO alignment gap before encoded audio.")

            roots = tuple(
                root for root, amount in enumerate(root_counts) for _ in range(amount)
            )
            recorded_roots = set(roots)
            for key, root in enumerate(body[0x8C:0x10C]):
                if root != 0xFF and root not in recorded_roots:
                    raise FormatError(
                        f"NPNO key {key} maps to root {root}, which has no strokes."
                    )
            strokes: list[NpnoStroke] = []
            cursor = audio_at
            unknown_banks: set[int] = set()
            for index, root in enumerate(roots):
                record_at = CNSP_PREFIX_SIZE + index * STROKE_RECORD_SIZE
                record = bytes(body[record_at : record_at + STROKE_RECORD_SIZE])
                declared_start = _u32be(record, 0)
                if declared_start != cursor:
                    raise FormatError(
                        f"NPNO stroke {index} starts at 0x{declared_start:X}; "
                        f"expected 0x{cursor:X}."
                    )
                bank = int(record[4])
                if bank not in {0, 1, 2}:
                    unknown_banks.add(bank)
                blocks = _u16be(record, 10)
                audio_size = blocks * block_size
                audio_end = cursor + audio_size
                if audio_end > len(body):
                    raise FormatError(f"Truncated encoded audio for NPNO stroke {index}.")
                seed_values = struct.unpack_from(">8h", record, 0x0C)
                strokes.append(
                    NpnoStroke(
                        index=index,
                        root=root,
                        bank=bank,
                        layer=int(record[5]),
                        frames=_u32be(record, 6),
                        blocks=blocks,
                        seeds=(
                            (seed_values[0], seed_values[1], seed_values[2], seed_values[3]),
                            (seed_values[4], seed_values[5], seed_values[6], seed_values[7]),
                        ),
                        trim_tenths_db=int(struct.unpack_from(">h", record, 0x34)[0]),
                        audio_offset=CBIN_HEADER_SIZE + cursor,
                        audio_size=audio_size,
                    )
                )
                cursor = audio_end
            if cursor != len(body):
                raise FormatError(
                    f"NPNO audio ends at 0x{cursor:X}, but the body ends at 0x{len(body):X}."
                )

            combined_name = _text(body, 0x1C, 0x20)
            name, separator, variant = combined_name.partition("#")
            warnings = [
                (
                    "Nord pedal-resonance strokes cannot be triggered equivalently by a "
                    "MODX AWM2 element and are omitted from X8L conversion."
                ),
                (
                    "Nord velocity layers are translated to evenly divided MODX velocity "
                    "ranges, and per-key tuning is represented at each sampled root; "
                    "validate the resulting response on the keyboard."
                ),
            ]
            if unknown_banks:
                warnings.append(
                    "Unknown NPNO stroke banks were preserved as metadata only: "
                    + ", ".join(str(value) for value in sorted(unknown_banks))
                    + "."
                )
            return NpnoLibrary(
                source_path=source_path,
                source_name=source_path.name,
                source_size=source_size,
                header=NpnoHeader(container_version, stream_version, stored_crc, False),
                name=" ".join(name.split()),
                variant=" ".join(variant.split()) if separator else "",
                channels=channels,
                key_map=tuple(int(value) for value in body[0x8C:0x10C]),
                fine_tune=tuple(struct.unpack_from(">128b", body, 0x18C)),
                gain_tenths_db=int(struct.unpack_from(">b", body, 0x40C)[0]),
                damper_top=int(body[0x40D]),
                strokes=tuple(strokes),
                warnings=tuple(warnings),
            )
        finally:
            body.release()


def decode_stroke(source: mmap.mmap, stroke: NpnoStroke, channels: int) -> np.ndarray:
    """Decode one stroke into a ``(frames, channels)`` int16 NumPy array."""
    if channels not in {1, 2}:
        raise FormatError(f"Cannot decode an NPNO stroke with {channels} channels.")
    block_size = BLOCK_WORDS * 2 * channels
    raw_audio = memoryview(source)[stroke.audio_offset : stroke.audio_offset + stroke.audio_size]
    histories = [list(reversed(stroke.seeds[channel])) for channel in range(channels)]
    lanes: list[list[np.ndarray]] = [[] for _ in range(channels)]
    previous_tail: list[np.ndarray] | None = None
    decoded_frames = 0
    coefficients = (
        (),
        (-1,),
        (-2, 1),
        (-3, 3, -1),
        (-4, 6, -4, 1),
    )
    try:
        for block_index in range(stroke.blocks):
            start = block_index * block_size
            raw = raw_audio[start : start + block_size]
            header = _u16be(raw, 0)
            width = header & 0x1F
            order = (header >> 5) & 0x07
            if not 1 <= width <= 16 or order > 4:
                raise FormatError(
                    f"NPNO stroke {stroke.index}, block {block_index}: "
                    f"invalid residual width/order {width}/{order}."
                )
            block_frames = 8 * (block_size - 2) // (width * channels)
            if block_frames < BLOCK_OVERLAP + 4:
                raise FormatError(
                    f"NPNO stroke {stroke.index}, block {block_index} is too short."
                )
            owned = block_frames - BLOCK_OVERLAP
            words = np.frombuffer(raw[2:], dtype=">u2").astype(np.uint64)
            field_count = block_frames * channels
            bit_at = np.arange(field_count, dtype=np.uint64) * width
            word_at = (bit_at >> 4).astype(np.int64)
            shifts = bit_at & 15
            padded = np.empty(words.size + 1, dtype=np.uint64)
            padded[:-1] = words
            padded[-1] = 0
            packed = (padded[word_at] >> shifts) | (padded[word_at + 1] << (16 - shifts))
            mask = (1 << width) - 1
            values = (packed & mask).astype(np.int64)
            sign = 1 << (width - 1)
            values = (values ^ sign) - sign

            block_lanes: list[np.ndarray] = []
            for channel in range(channels):
                residual = values[channel::channels]
                history = histories[channel]
                if order == 0:
                    decoded = residual
                else:
                    decoded = residual.copy()
                    differences: list[int] = [history[0]]
                    samples = history
                    for degree in range(1, order):
                        value = 0
                        row = (1, *coefficients[degree])
                        for coefficient, sample in zip(row, samples, strict=False):
                            value += coefficient * sample
                        differences.append(value)
                    for degree in range(order - 1, -1, -1):
                        decoded = np.cumsum(decoded, dtype=np.int64) + differences[degree]
                decoded16 = decoded.clip(-32768, 32767).astype(np.int16)
                if previous_tail is not None and not np.array_equal(
                    decoded16[:BLOCK_OVERLAP], previous_tail[channel]
                ):
                    raise FormatError(
                        f"NPNO stroke {stroke.index}, block {block_index} has a broken overlap."
                    )
                block_lanes.append(decoded16)
                histories[channel] = [int(value) for value in decoded16[owned - 4 : owned][::-1]]
                lanes[channel].append(decoded16[:owned].copy())
            previous_tail = [lane[owned:].copy() for lane in block_lanes]
            decoded_frames += owned
    finally:
        raw_audio.release()
    if decoded_frames != stroke.frames:
        raise FormatError(
            f"NPNO stroke {stroke.index} decoded {decoded_frames} frames; "
            f"the directory declares {stroke.frames}."
        )
    return np.column_stack([np.concatenate(lane) for lane in lanes])


def resample_pcm(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linearly resample signed PCM while keeping channels separate."""
    if source_rate == target_rate:
        return audio
    source_frames = int(audio.shape[0])
    target_frames = round(source_frames * target_rate / source_rate)
    if source_frames == 0 or target_frames == 0:
        return np.empty((0, int(audio.shape[1])), dtype=np.int16)
    positions = np.arange(target_frames, dtype=np.float64) * source_rate / target_rate
    left = np.minimum(positions.astype(np.int64), source_frames - 1)
    right = np.minimum(left + 1, source_frames - 1)
    fraction = positions - left
    result = np.empty((target_frames, int(audio.shape[1])), dtype=np.int16)
    for channel in range(int(audio.shape[1])):
        values = audio[:, channel].astype(np.float64)
        interpolated = values[left] + (values[right] - values[left]) * fraction
        result[:, channel] = np.rint(interpolated).clip(-32768, 32767).astype(np.int16)
    return result


def _crc32_body(path: Path) -> int:
    checksum = 0
    with path.open("rb") as stream:
        stream.seek(CBIN_HEADER_SIZE)
        while chunk := stream.read(4 * 1024 * 1024):
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def _first_audio_offset(directory_end: int, block_size: int) -> int:
    return ((directory_end + AUDIO_ALIGN_BIAS + block_size - 1) // block_size) * block_size - AUDIO_ALIGN_BIAS


def _u16be(data: bytes | memoryview, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise FormatError("Truncated NPNO 16-bit field.")
    return int(struct.unpack_from(">H", data, offset)[0])


def _u32be(data: bytes | memoryview, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise FormatError("Truncated NPNO 32-bit field.")
    return int(struct.unpack_from(">I", data, offset)[0])


def _text(data: memoryview, offset: int, size: int) -> str:
    raw = bytes(data[offset : offset + size]).split(b"\x00", 1)[0]
    return raw.decode("ascii", errors="replace").strip()
