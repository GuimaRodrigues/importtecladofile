"""Structural scanner for classic X7/X8 DPFM Waveform references.

The framing and field locations implemented here are interoperability facts
cross-checked against real MODX data, YSFC Forge, and ConvertWithMoss.  This is
an independent, minimal bounds-checked implementation; no third-party source
code is included.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import DependencyError, FormatError

AWM2_PART = 0
DRUM_PART = 1
FMX_PART = 2


@dataclass(frozen=True, slots=True)
class ClassicWaveformReference:
    part_index: int
    element_index: int
    bank_position: int
    number_position: int
    bank: int
    number: int

    @property
    def program_number(self) -> int:
        return (self.bank << 16) | self.number


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def read_u32be(self, context: str) -> int:
        self.require(4, context)
        value = struct.unpack_from(">I", self.data, self.position)[0]
        self.position += 4
        return int(value)

    def require(self, size: int, context: str) -> None:
        if size < 0 or self.position + size > len(self.data):
            raise FormatError(
                f"Truncated DPFM while reading {context}: position {self.position}, "
                f"{size} bytes required, {self.remaining} available."
            )

    def data_block(self, context: str) -> tuple[int, int]:
        size = self.read_u32be(f"{context} size")
        self.require(size, context)
        start = self.position
        self.position += size
        return start, size


def scan_classic_waveform_references(blob: bytes) -> tuple[ClassicWaveformReference, ...]:
    """Locate every AWM2/Drum Waveform bank and little-endian number field."""
    cursor = _Cursor(blob)
    cursor.data_block("Performance Common")
    for effect in ("Reverb", "Variation", "Master EQ", "Master Effect"):
        cursor.data_block(effect)

    part_count = cursor.read_u32be("Part count")
    if part_count > 16:
        raise FormatError(f"DPFM declares {part_count} Parts; the known maximum is 16.")
    for part_index in range(part_count):
        cursor.data_block(f"Part {part_index + 1} header")
    cursor.data_block("AD Part")
    cursor.data_block("Digital Input Part")

    references: list[ClassicWaveformReference] = []
    for part_index in range(part_count):
        part_type = cursor.read_u32be(f"Part {part_index + 1} type")
        item_count = cursor.read_u32be(f"Part {part_index + 1} element count")
        if item_count > 128:
            raise FormatError(
                f"Part {part_index + 1} declares {item_count} elements/operators; "
                "the known maximum is 128."
            )
        if part_type in (AWM2_PART, DRUM_PART):
            for element_index in range(item_count):
                start, size = cursor.data_block(
                    f"element {element_index + 1} of Part {part_index + 1}"
                )
                if size < 4:
                    raise FormatError(
                        f"Element {element_index + 1} of Part {part_index + 1} "
                        f"has only {size} bytes."
                    )
                bank_position = start + 1
                number_position = start + size - 2
                bank = blob[bank_position]
                number = int(struct.unpack_from("<H", blob, number_position)[0])
                if bank > 9:
                    raise FormatError(
                        f"Invalid Waveform bank in Part {part_index + 1}, "
                        f"element {element_index + 1}: {bank}."
                    )
                references.append(
                    ClassicWaveformReference(
                        part_index=part_index,
                        element_index=element_index,
                        bank_position=bank_position,
                        number_position=number_position,
                        bank=bank,
                        number=number,
                    )
                )
        elif part_type == FMX_PART:
            cursor.data_block(f"FM-X Common for Part {part_index + 1}")
            for operator_index in range(item_count):
                cursor.data_block(
                    f"FM-X operator {operator_index + 1} of Part {part_index + 1}"
                )
        else:
            raise FormatError(
                f"Unknown classic Part type in DPFM: {part_type} "
                f"(Part {part_index + 1})."
            )
    # Remaining bytes are the opaque play-settings area (including arpeggios).
    return tuple(references)


def remap_classic_waveform_references(blob: bytes, mapping: dict[int, int]) -> bytes:
    """Rewrite all non-preset element references and verify the resulting blob."""
    references = scan_classic_waveform_references(blob)
    result = bytearray(blob)
    expected: list[int] = []
    for reference in references:
        if reference.bank == 0:
            expected.append(reference.program_number)
            continue
        target = mapping.get(reference.program_number)
        if target is None:
            raise DependencyError(
                f"DPFM references Waveform 0x{reference.program_number:08X}, "
                "but it is not in the dependency closure."
            )
        target_bank = target >> 16
        target_number = target & 0xFFFF
        if not 1 <= target_bank <= 9 or not 1 <= target_number <= 0xFFFF:
            raise DependencyError(f"Invalid Waveform target: 0x{target:08X}.")
        result[reference.bank_position] = target_bank
        struct.pack_into("<H", result, reference.number_position, target_number)
        expected.append(target)

    rewritten = bytes(result)
    verified = scan_classic_waveform_references(rewritten)
    actual = [item.program_number for item in verified]
    if actual != expected:
        raise DependencyError("DPFM Waveform remapping verification failed.")
    return rewritten
