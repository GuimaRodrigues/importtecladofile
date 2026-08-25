"""Small, sample-free diagnostics for remotely reported YSFC files."""

from __future__ import annotations

import struct
from pathlib import Path

MAGIC = b"YAMAHA-YSFC"
HEADER_SIZE = 64
MAX_DIAGNOSTIC_CATALOGUE_SIZE = 64 * 1024


def probe_ysfc_file(path: str | Path) -> dict[str, object]:
    """Read only the fixed header and bounded catalogue; never read sample payloads."""
    source = Path(path)
    result: dict[str, object] = {"name": source.name}
    try:
        size = source.stat().st_size
        result["size"] = size
        with source.open("rb") as stream:
            header = stream.read(HEADER_SIZE)
            result["header_bytes_read"] = len(header)
            if len(header) < HEADER_SIZE:
                result["probe_error"] = "Header is shorter than 64 bytes."
                return result

            signature = header[0:16].rstrip(b"\x00")
            result["signature"] = signature.decode("ascii", errors="replace")
            version_raw = header[16:32].split(b"\x00", 1)[0].rstrip(b"\xFF")
            result["ysfc_version"] = version_raw.decode("ascii", errors="replace")
            catalogue_size = struct.unpack_from(">I", header, 32)[0]
            result["catalogue_size"] = catalogue_size
            result["declared_library_info_size"] = struct.unpack_from(">I", header, 48)[0]
            result["header_value_at_60"] = struct.unpack_from(">I", header, 60)[0]

            if signature != MAGIC:
                result["probe_error"] = "YAMAHA-YSFC signature is missing."
                return result
            if catalogue_size % 8:
                result["probe_error"] = "Catalogue size is not a multiple of 8."
                return result
            if catalogue_size > MAX_DIAGNOSTIC_CATALOGUE_SIZE:
                result["probe_error"] = "Catalogue is too large for a safe diagnostic probe."
                return result
            if HEADER_SIZE + catalogue_size > size:
                result["probe_error"] = "Catalogue extends beyond the end of the file."
                return result

            catalogue = stream.read(catalogue_size)
            entries: list[dict[str, object]] = []
            for position in range(0, len(catalogue), 8):
                chunk_id = catalogue[position : position + 4].decode(
                    "ascii", errors="replace"
                )
                offset = struct.unpack_from(">I", catalogue, position + 4)[0]
                entries.append({"id": chunk_id, "offset": offset})
            result["catalogue"] = entries
    except OSError as exc:
        result["probe_error"] = str(exc)
    return result
