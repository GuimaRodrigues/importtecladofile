# This module includes a ConvertWithMoss Performance template and compatible
# structure-writing logic. It is distributed under LGPL-3.0-only; see
# LICENSES/LGPL-3.0-only.txt and NOTICE.md.
"""Convert a Nord Piano Library into an experimental MODX/MODX+ X8L.

The YSFC waveform/keybank layout and the compressed default AWM2 Performance
template are interoperable with the LGPL-3.0 ConvertWithMoss implementation.
This module stays deliberately small and exposes its generated structures through
the normal project parser for round-trip verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mmap
import os
import struct
import tempfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .dependencies import DependencyResolver
from .errors import ExportError, FormatError
from .exporter import ExportResult
from .npno import SAMPLE_RATE, NpnoLibrary, NpnoStroke, decode_stroke, resample_pcm
from .parser import YamahaLibraryParser

TARGET_RATE = 44_100
MAX_WAVEFORM_CHANNEL_ELEMENTS = 255
MAX_PERFORMANCE_ELEMENTS = 8
MODX_WAVEFORM_MEMORY = 1024 * 1024 * 1024

# ConvertWithMoss InitPerf501.bin (6,748 bytes), zlib-compressed and base85 encoded.
# The uncompressed template is an initialized one-Part AWM2 Performance. Keeping it
# encoded makes the source distribution text-only while retaining exact template bytes.
_PERFORMANCE_TEMPLATE_B85 = (
    "c-rk)!EVz)5PfT>vDFlsmQr#6$wCMfflwPlicp}_o+uK6Lq(ydNLYeidMVtH)<1wa_0CuDN%SK)"
    "z?*fFx~@S{4sBHJv%IsO^~}!h%&d0}1lT^<|D0^@zG|b08sr3441s3Q=)c{4_kLmV+S2vq8!c-^"
    "OhHCafvI68C0rE4#9x6KppV#0MFn`KUJRgRf?3?h6|CVFVnkTS|6xR@cq3#n4=t$@U2Nk6mO1+mv"
    "Bl@xtkw19Dg}LkC0#T!7eSUHxf#iWNIH?WZp5@Gq-{VGd#v4KH#?*&jL@8RID|T0SNOge*}T)>cX"
    "g0A!LYWlThB#O$>v}MjA*0mf1!{TUHK+Bmcg)+1`8_zjji#j7Zb=qTRGNLoKa^K14DZL&~8`7-NI="
    "j!&;=G6Otnf)6@-Ndbyj2hx;E7lC@Ey4H)e*<58K>DKj3I8Qn5tz07zrV!#X&Yf37>_fUh*I#>T8"
    "CJU0uBr8J;hzceVG4CUn0$eIwsu*pv_H3dwG)qIiWZP`FHIbIwS?#^Z2$yF4o?t~IL$Sm5Kzg+wR"
    "Ez8vR|R>KaUj_z*U%bDzGR<WIp&8Bcb!G#4Ogrqc*DG5-Y{?2IEI;@3%anKz6$Fhb%wN(R?Syz5z"
    "3g!2kXeV5+}q<j4wf41x~+L|DyZ7k)rc(o|l+J!h6Ph#!Gn5ToTV*#D~Z40Vditc+Ysxc+YsxT$VG"
    "?tUJqQA@=m?Y2_ciFh6gTrfD|NnV+8v_zUnT{Vf"
)


@dataclass(frozen=True, slots=True)
class NpnoWaveformPlan:
    index: int
    bank: int
    strokes: tuple[NpnoStroke, ...]
    name: str

    @property
    def program_number(self) -> int:
        return 0x00010000 + self.index


@dataclass(frozen=True, slots=True)
class NpnoSelection:
    waveform_plans: tuple[NpnoWaveformPlan, ...]
    estimated_pcm_bytes: int
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    performance_indices: tuple[int, ...] = (0,)

    @property
    def can_export(self) -> bool:
        return not self.blockers


class NpnoResolver:
    def __init__(self, library: NpnoLibrary) -> None:
        self.library = library

    def resolve(self, indices: tuple[int, ...] | list[int]) -> NpnoSelection:
        if set(indices) != {0}:
            raise ExportError("The NPNO source contains one instrument; select that item.")
        plans: list[NpnoWaveformPlan] = []
        short_name = _ascii_name(self.library.name, 14)
        for bank, label in ((0, "Attack"), (2, "Release")):
            strokes = tuple(stroke for stroke in self.library.strokes if stroke.bank == bank)
            for group in _split_at_root_boundaries(
                strokes, MAX_WAVEFORM_CHANNEL_ELEMENTS // self.library.channels
            ):
                index = len(plans) + 1
                plans.append(
                    NpnoWaveformPlan(index, bank, group, f"{short_name} {label[0]}{index}")
                )

        blockers: list[str] = []
        if not self.library.attack_strokes:
            blockers.append("The NPNO file has no attack strokes.")
        if len(plans) > MAX_PERFORMANCE_ELEMENTS:
            blockers.append(
                f"Conversion needs {len(plans)} AWM2 elements, but a Part supports "
                f"only {MAX_PERFORMANCE_ELEMENTS}."
            )
        pcm_bytes = sum(
            _target_frames(stroke.frames) * self.library.channels * 2
            for plan in plans
            for stroke in plan.strokes
        )
        if pcm_bytes > MODX_WAVEFORM_MEMORY:
            blockers.append(
                f"Decoded samples require about {pcm_bytes:,} bytes, above the 1 GiB "
                "MODX/MODX+ waveform-memory limit."
            )
        return NpnoSelection(tuple(plans), pcm_bytes, self.library.warnings, tuple(blockers))


class NpnoX8lExporter:
    def __init__(self, library: NpnoLibrary) -> None:
        self.library = library

    def estimate_size(self, selection: NpnoSelection) -> int:
        chunks = self._small_chunks(selection)
        dwim_size = self._dwim_size(selection)
        catalogue_size = (len(chunks) + 1) * 8
        return 64 + catalogue_size + 81 + sum(len(value) for value in chunks.values()) + dwim_size

    def export(
        self,
        selection: NpnoSelection,
        output_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> ExportResult:
        if not selection.can_export:
            raise ExportError(
                "NPNO conversion is blocked:\n"
                + "\n".join(f"- {value}" for value in selection.blockers)
            )
        output = Path(output_path).resolve()
        if output.suffix.upper() != ".X8L":
            raise ExportError("NPNO conversion output must use the .X8L extension.")
        if output == self.library.source_path:
            raise ExportError("The source file must never be overwritten.")
        report_path = output.with_suffix(output.suffix + ".json")
        if not overwrite:
            existing = [str(path) for path in (output, report_path) if path.exists()]
            if existing:
                raise ExportError("Destination already exists: " + ", ".join(existing) + ".")
        output.parent.mkdir(parents=True, exist_ok=True)

        output_temp = _temporary_path(output, ".X8L")
        report_temp = _temporary_path(report_path, ".json")
        digest = hashlib.sha256()
        try:
            with output_temp.open("wb") as raw_target:
                target = _HashingWriter(raw_target, digest)
                self._write_file(target, selection)
                raw_target.flush()
                os.fsync(raw_target.fileno())
            output_size = output_temp.stat().st_size
            expected = self.estimate_size(selection)
            if output_size != expected:
                raise ExportError(
                    f"Internal NPNO conversion size mismatch: {output_size} != {expected}."
                )

            reopened = YamahaLibraryParser().parse(output_temp)
            if [item.name for item in reopened.performances] != [_performance_name(self.library)]:
                raise ExportError("Round-trip validation failed for the generated Performance.")
            closure = DependencyResolver(reopened).resolve((0,))
            if not closure.can_export or closure.waveform_count != len(selection.waveform_plans):
                raise ExportError("Dependency validation failed for the generated X8L.")

            report = self._report(selection, output_size, digest.hexdigest())
            with report_temp.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(report_temp, report_path)
            os.replace(output_temp, output)
            return ExportResult(output, report_path, output_size, digest.hexdigest(), report)
        except (ExportError, FormatError):
            raise
        except OSError as exc:
            raise ExportError(f"Failed to create the X8L: {exc}") from exc
        finally:
            output_temp.unlink(missing_ok=True)
            report_temp.unlink(missing_ok=True)

    def _small_chunks(self, selection: NpnoSelection) -> dict[str, bytes]:
        performance = _performance_name(self.library)
        references = tuple(plan.program_number for plan in selection.waveform_plans)
        dpfm = _performance_payload(self.library, performance, selection.waveform_plans)
        sample_numbers = _sample_numbers(selection.waveform_plans)
        wfm_payloads = tuple(
            self._waveform_metadata(plan, sample_numbers) for plan in selection.waveform_plans
        )
        wim_sizes = tuple(self._waveform_image_payload_size(plan) for plan in selection.waveform_plans)

        next_id = 10_001
        epfm_entry = _entry(
            item_size=len(dpfm),
            data_offset=12,
            program=0x003F2000,
            flags=b"\x00\x00\x00\x02\x00\x01",
            entry_id=next_id,
            name=f"0:{performance}",
            title=performance,
            additional=b"".join(struct.pack(">I", value) for value in references),
        )
        next_id += 1
        ewfm_entries: list[bytes] = []
        ewim_entries: list[bytes] = []
        wfm_offset = 12
        wim_offset = 12
        for plan, metadata, image_size in zip(
            selection.waveform_plans, wfm_payloads, wim_sizes, strict=True
        ):
            ewfm_entries.append(
                _entry(
                    item_size=len(metadata),
                    data_offset=wfm_offset,
                    program=plan.program_number,
                    flags=b"\x00" * 6,
                    entry_id=next_id,
                    name=f"0:{plan.name}",
                    title="",
                    additional=b"",
                )
            )
            next_id += 1
            ewim_entries.append(
                _entry(
                    item_size=image_size,
                    data_offset=wim_offset,
                    program=plan.program_number,
                    flags=b"\x00" * 6,
                    entry_id=next_id,
                    name=f"0:{plan.name}",
                    title="",
                    additional=b"",
                )
            )
            next_id += 1
            wfm_offset += 8 + len(metadata)
            wim_offset += 8 + image_size

        result = {
            "EPFM": _entry_chunk("EPFM", (epfm_entry,)),
            "EWFM": _entry_chunk("EWFM", tuple(ewfm_entries)),
            "EWIM": _entry_chunk("EWIM", tuple(ewim_entries)),
            "DPFM": _data_chunk("DPFM", (dpfm,)),
            "DWFM": _data_chunk("DWFM", wfm_payloads),
        }
        for chunk_id in (
            "EARP", "DARP", "ESOM", "ESPG", "DSOM", "DSPG", "ECRV", "DCRV", "ELST", "DLST"
        ):
            result[chunk_id] = _empty_chunk(chunk_id)
        return result

    def _write_file(self, target: _HashingWriter, selection: NpnoSelection) -> None:
        small = self._small_chunks(selection)
        order = ("EPFM", "EWFM", "EWIM", "DPFM", "DWFM", "DWIM", "EARP", "DARP", "ESOM", "ESPG", "DSOM", "DSPG", "ECRV", "DCRV", "ELST", "DLST")
        sizes = {chunk_id: len(small[chunk_id]) for chunk_id in small}
        sizes["DWIM"] = self._dwim_size(selection)
        catalogue_size = len(order) * 8
        start = 64 + catalogue_size + 81
        offsets: dict[str, int] = {}
        cursor = start
        for chunk_id in order:
            offsets[chunk_id] = cursor
            cursor += sizes[chunk_id]
        if cursor >= 1 << 32:
            raise ExportError("Generated X8L exceeds the 32-bit YSFC offset range.")

        header = bytearray(b"\xFF" * 64)
        header[0:16] = b"YAMAHA-YSFC" + b"\x00" * 5
        header[16:32] = b"5.0.1" + b"\x00" * 11
        struct.pack_into(">I", header, 32, catalogue_size)
        struct.pack_into(">I", header, 48, 81)
        struct.pack_into(">I", header, 60, 10_002 + 2 * len(selection.waveform_plans))
        target.write(header)
        for chunk_id in order:
            target.write(chunk_id.encode("ascii") + struct.pack(">I", offsets[chunk_id]))
        target.write(b"\xFF" * 80 + b"\x00")

        source_file = self.library.source_path.open("rb")
        source_map = mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for chunk_id in order:
                if chunk_id == "DWIM":
                    self._write_dwim(target, selection, source_map)
                else:
                    target.write(small[chunk_id])
        finally:
            source_map.close()
            source_file.close()

    def _waveform_metadata(
        self, plan: NpnoWaveformPlan, sample_number_by_index: dict[int, int]
    ) -> bytes:
        ranges = _stroke_ranges(self.library, plan)
        records = bytearray()
        for stroke in plan.strokes:
            low_key, high_key, low_velocity, high_velocity = ranges[stroke.index]
            frames = _target_frames(stroke.frames)
            fine_cents = round(self.library.fine_tune[stroke.root] * 0.7)
            fine_raw = max(0, min(127, round(fine_cents / 1.5625 + 64)))
            combined_gain_db = (
                self.library.gain_tenths_db - stroke.trim_tenths_db
            ) / 10.0
            level = _level_from_gain(min(0.0, combined_gain_db))
            record = bytearray(64)
            record[0:12] = bytes(
                (low_key, high_key, low_velocity, high_velocity, level, 64, 0, 0xFF, stroke.root, 64, fine_raw, self.library.channels)
            )
            record[12:20] = bytes((0, 2, 5, 1, 0, 0, 0, 1))
            record[20:32] = b"\x00" * 12
            record[32:36] = b"\x00\x00\x00\xFF"
            struct.pack_into("<IIII", record, 36, TARGET_RATE, 0, 0, frames)
            struct.pack_into("<I", record, 52, 0)
            struct.pack_into("<II", record, 56, sample_number_by_index[stroke.index], frames)
            for _channel in range(self.library.channels):
                records.extend(record)
        return struct.pack("<H", len(plan.strokes) * self.library.channels) + b"\x00\x00" + bytes(records)

    def _waveform_image_payload_size(self, plan: NpnoWaveformPlan) -> int:
        return 4 + sum(
            self.library.channels * (4 + _target_frames(stroke.frames) * 2)
            for stroke in plan.strokes
        )

    def _dwim_size(self, selection: NpnoSelection) -> int:
        return 12 + sum(8 + self._waveform_image_payload_size(plan) for plan in selection.waveform_plans)

    def _write_dwim(
        self, target: _HashingWriter, selection: NpnoSelection, source: mmap.mmap
    ) -> None:
        total = self._dwim_size(selection)
        target.write(b"DWIM" + struct.pack(">II", total - 8, len(selection.waveform_plans)))
        for plan in selection.waveform_plans:
            payload_size = self._waveform_image_payload_size(plan)
            target.write(b"Data" + struct.pack(">I", payload_size))
            target.write(struct.pack(">I", len(plan.strokes) * self.library.channels))
            for stroke in plan.strokes:
                audio = resample_pcm(
                    decode_stroke(source, stroke, self.library.channels),
                    SAMPLE_RATE,
                    TARGET_RATE,
                )
                gain_db = max(
                    0.0,
                    (self.library.gain_tenths_db - stroke.trim_tenths_db) / 10.0,
                )
                if gain_db != 0.0:
                    import numpy as np

                    gain = 10.0 ** (gain_db / 20.0)
                    audio = (
                        np.rint(audio.astype(np.float32) * gain)
                        .clip(-32768, 32767)
                        .astype("<i2")
                    )
                for channel in range(self.library.channels):
                    channel_data = audio[:, channel].astype("<i2", copy=False).tobytes()
                    target.write(struct.pack(">I", len(channel_data)))
                    target.write(channel_data)

    def _report(
        self, selection: NpnoSelection, output_size: int, sha256: str
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "source": {
                "name": self.library.source_name,
                "size": self.library.source_size,
                "format": "Nord Piano Library (NPNO)",
                "container_version": self.library.header.container_version,
                "stream_version": f"0x{self.library.header.stream_version:03X}",
                "checksum_verified": self.library.header.checksum_ok,
            },
            "output": {
                "family": "X8",
                "ysfc_version": "5.0.1",
                "extension": ".X8L",
                "size": output_size,
                "sha256": sha256,
            },
            "conversion": {
                "performance": _performance_name(self.library),
                "sample_rate_source": SAMPLE_RATE,
                "sample_rate_output": TARGET_RATE,
                "attack_strokes": len(self.library.attack_strokes),
                "release_strokes": len(self.library.release_strokes),
                "resonance_strokes_omitted": len(self.library.resonance_strokes),
                "waveforms": len(selection.waveform_plans),
                "decoded_pcm_bytes": selection.estimated_pcm_bytes,
            },
            "warnings": list(selection.warnings),
            "hardware_validation": "not_performed",
        }


def prepare_exporter(library: NpnoLibrary, selection: NpnoSelection) -> NpnoX8lExporter:
    """Create the NPNO exporter (kept as a factory for CLI/GUI symmetry)."""
    del selection
    return NpnoX8lExporter(library)


def _split_at_root_boundaries(
    strokes: tuple[NpnoStroke, ...], maximum_strokes: int
) -> tuple[tuple[NpnoStroke, ...], ...]:
    if maximum_strokes < 1:
        return ()
    roots: list[list[NpnoStroke]] = []
    for stroke in strokes:
        if not roots or roots[-1][0].root != stroke.root:
            roots.append([])
        roots[-1].append(stroke)
    result: list[tuple[NpnoStroke, ...]] = []
    current: list[NpnoStroke] = []
    for group in roots:
        if current and len(current) + len(group) > maximum_strokes:
            result.append(tuple(current))
            current = []
        if len(group) > maximum_strokes:
            for start in range(0, len(group), maximum_strokes):
                if current:
                    result.append(tuple(current))
                    current = []
                result.append(tuple(group[start : start + maximum_strokes]))
        else:
            current.extend(group)
    if current:
        result.append(tuple(current))
    return tuple(result)


def _sample_numbers(plans: tuple[NpnoWaveformPlan, ...]) -> dict[int, int]:
    result: dict[int, int] = {}
    number = 1
    for plan in plans:
        for stroke in plan.strokes:
            result[stroke.index] = number
            number += 1
    return result


def _stroke_ranges(
    library: NpnoLibrary, plan: NpnoWaveformPlan
) -> dict[int, tuple[int, int, int, int]]:
    keys_by_root: dict[int, list[int]] = {}
    for key, root in enumerate(library.key_map):
        if root != 0xFF:
            keys_by_root.setdefault(root, []).append(key)
    layers_by_root: dict[int, list[NpnoStroke]] = {}
    for stroke in plan.strokes:
        layers_by_root.setdefault(stroke.root, []).append(stroke)
    result: dict[int, tuple[int, int, int, int]] = {}
    for root, strokes in layers_by_root.items():
        ordered = sorted(strokes, key=lambda item: (item.layer, item.index))
        velocities: dict[int, list[int]] = {stroke.index: [] for stroke in ordered}
        for velocity in range(1, 128):
            layer_index = min(len(ordered) - 1, (velocity - 1) * len(ordered) // 127)
            selected = ordered[layer_index]
            velocities[selected.index].append(velocity)
        covered_keys = keys_by_root.get(root, [root])
        for stroke in ordered:
            values = velocities[stroke.index]
            if not values:
                raise ExportError("Too many NPNO velocity layers for one MODX Waveform.")
            result[stroke.index] = (
                min(covered_keys),
                max(covered_keys),
                min(values),
                max(values),
            )
    return result


def _performance_payload(
    library: NpnoLibrary, name: str, plans: tuple[NpnoWaveformPlan, ...]
) -> bytes:
    data = bytearray(zlib.decompress(base64.b85decode(_PERFORMANCE_TEMPLATE_B85)))
    cursor = 0

    def block() -> tuple[int, int]:
        nonlocal cursor
        size = struct.unpack_from(">I", data, cursor)[0]
        start = cursor + 4
        cursor = start + size
        if cursor > len(data):
            raise ExportError("The embedded MODX Performance template is truncated.")
        return start, size

    common_start, _ = block()
    _write_name(data, common_start, name)
    for _ in range(4):
        block()
    part_count = struct.unpack_from(">I", data, cursor)[0]
    cursor += 4
    if part_count != 1:
        raise ExportError("The embedded MODX template does not contain exactly one Part.")
    part_start, _ = block()
    _write_name(data, part_start, name)
    data[part_start + 22] = 0
    data[part_start + 23] = 0
    block()
    block()
    part_type, element_count = struct.unpack_from(">II", data, cursor)
    cursor += 8
    if part_type != 0 or element_count != 8:
        raise ExportError("The embedded MODX template is not an eight-element AWM2 Part.")
    for element_index in range(element_count):
        start, size = block()
        if size < 16:
            raise ExportError("The embedded MODX template has a truncated AWM2 element.")
        if element_index < len(plans):
            plan = plans[element_index]
            data[start] = 1
            data[start + 1] = 1
            data[start + 10] = 2 if plan.bank == 2 else 0
            roots = {stroke.root for stroke in plan.strokes}
            covered_keys = [
                key for key, root in enumerate(library.key_map) if root in roots
            ]
            if not covered_keys:
                covered_keys = sorted(roots)
            data[start + 11] = min(covered_keys)
            data[start + 12] = max(covered_keys)
            struct.pack_into("<H", data, start + size - 2, plan.index)
        else:
            data[start] = 0
            data[start + 1] = 0
            struct.pack_into("<H", data, start + size - 2, 0)
    return bytes(data)


def _write_name(data: bytearray, offset: int, name: str) -> None:
    encoded = _ascii_name(name, 20).encode("ascii")
    data[offset : offset + 21] = encoded + b"\x00" * (21 - len(encoded))


def _performance_name(library: NpnoLibrary) -> str:
    return _ascii_name(library.name, 20)


def _ascii_name(value: str, limit: int) -> str:
    return value.encode("ascii", errors="replace").decode("ascii")[:limit].rstrip() or "NPNO Piano"


def _target_frames(frames: int) -> int:
    return round(frames * TARGET_RATE / SAMPLE_RATE)


def _level_from_gain(gain: float) -> int:
    if gain < -95.25:
        return 0
    return max(0, min(255, round((max(-95.25, min(0.0, gain)) + 95.25) / 0.375) + 1))


def _entry(
    *, item_size: int, data_offset: int, program: int, flags: bytes, entry_id: int,
    name: str, title: str, additional: bytes
) -> bytes:
    body = (
        struct.pack(">III", item_size, data_offset, program)
        + flags
        + struct.pack(">I", entry_id)
        + name.encode("ascii", errors="replace") + b"\x00"
        + title.encode("ascii", errors="replace") + b"\x00"
        + additional
    )
    return b"Entr" + struct.pack(">I", len(body)) + body


def _entry_chunk(chunk_id: str, entries: tuple[bytes, ...]) -> bytes:
    payload = b"".join(entries)
    return chunk_id.encode("ascii") + struct.pack(">II", len(payload) + 4, len(entries)) + payload


def _data_chunk(chunk_id: str, payloads: tuple[bytes, ...]) -> bytes:
    records = b"".join(b"Data" + struct.pack(">I", len(value)) + value for value in payloads)
    return chunk_id.encode("ascii") + struct.pack(">II", len(records) + 4, len(payloads)) + records


def _empty_chunk(chunk_id: str) -> bytes:
    return chunk_id.encode("ascii") + struct.pack(">II", 4, 0)


def _temporary_path(destination: Path, suffix: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=suffix, dir=destination.parent)
    os.close(descriptor)
    return Path(name)


class _HashingWriter:
    def __init__(self, stream: BinaryIO, digest: object) -> None:
        self.stream = stream
        self.digest = digest

    def write(self, data: bytes | bytearray) -> None:
        self.stream.write(data)
        self.digest.update(data)  # type: ignore[attr-defined]
