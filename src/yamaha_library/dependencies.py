"""Dependency graph construction and conservative closure calculation."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

from .classic_dpfm import scan_classic_waveform_references
from .errors import DependencyError, FormatError
from .model import Block, EntryRecord, ParsedLibrary, Performance

POTENTIAL_DEPENDENCY_SUFFIXES = frozenset({"ARP", "MSQ", "CRV", "MTN", "PTN", "SPG", "SOM"})
NON_DEPENDENCY_SUFFIXES = frozenset({"LST", "PAT", "SNG", "SYS", "FVT", "PCH"})
DIRECTLY_INTERPRETED_SUFFIXES = frozenset({"PFM", "WFM", "WIM"})


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """Directed graph represented by stable textual resource identifiers."""

    adjacency: dict[str, frozenset[str]]

    def closure(self, roots: tuple[str, ...]) -> frozenset[str]:
        visited: set[str] = set()
        pending = deque(roots)
        while pending:
            node = pending.popleft()
            if node in visited:
                continue
            visited.add(node)
            pending.extend(self.adjacency.get(node, ()))
        return frozenset(visited)


@dataclass(frozen=True, slots=True)
class DependencySelection:
    performance_indices: tuple[int, ...]
    performance_programs: tuple[int, ...]
    performance_program_map: dict[int, int]
    target_performance_programs: tuple[int, ...]
    waveform_programs: tuple[int, ...]
    waveform_program_map: dict[int, int]
    waveform_indices: tuple[int, ...]
    waveform_image_indices: tuple[int, ...]
    shared_waveforms: dict[int, int]
    preserved_suffixes: tuple[str, ...]
    preserved_block_ids: tuple[str, ...]
    resource_counts: dict[str, int]
    graph: DependencyGraph
    closure: frozenset[str]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    conservative: bool

    @property
    def performance_count(self) -> int:
        return len(self.performance_indices)

    @property
    def waveform_count(self) -> int:
        return len(self.waveform_programs)

    @property
    def sample_count(self) -> int:
        return len(self.waveform_image_indices)

    @property
    def can_export(self) -> bool:
        return not self.blockers and self.performance_count > 0


class DependencyResolver:
    def __init__(self, library: ParsedLibrary) -> None:
        self.library = library

    def performance_indices_by_names(self, names: list[str] | tuple[str, ...]) -> tuple[int, ...]:
        if not names:
            raise DependencyError("Select at least one Performance.")
        by_name: dict[str, list[Performance]] = {}
        by_program: dict[int, Performance] = {}
        for performance in self.library.performances:
            by_name.setdefault(performance.name.casefold(), []).append(performance)
            by_program[performance.program_number] = performance
        indices: list[int] = []
        for requested in names:
            requested_program = _parse_program_number(requested)
            if requested_program is not None:
                matched_performance = by_program.get(requested_program)
                if matched_performance is None:
                    raise DependencyError(
                        f"Performance not found: program number 0x{requested_program:08X}."
                    )
                indices.append(matched_performance.index)
                continue
            matches = by_name.get(requested.casefold(), [])
            if not matches:
                raise DependencyError(f"Performance not found: '{requested}'.")
            if len(matches) > 1:
                programs = ", ".join(f"0x{item.program_number:08X}" for item in matches)
                raise DependencyError(
                    f"Ambiguous name '{requested}' ({programs}). Use the GUI or program number."
                )
            indices.append(matches[0].index)
        return tuple(dict.fromkeys(indices))

    def resolve(
        self,
        performance_indices: list[int] | tuple[int, ...],
        *,
        conservative: bool = True,
    ) -> DependencySelection:
        indices = tuple(dict.fromkeys(performance_indices))
        if not indices:
            raise DependencyError("Select at least one Performance.")
        performances = self.library.performances
        for index in indices:
            if not 0 <= index < len(performances):
                raise DependencyError(f"Performance index does not exist: {index}.")

        selected = tuple(performances[index] for index in indices)
        if len(selected) > 640:
            raise DependencyError("A classic Library supports at most 640 Performances.")
        duplicate_performance_programs = [
            program
            for program, count in Counter(item.program_number for item in selected).items()
            if count > 1
        ]
        if duplicate_performance_programs:
            values = ", ".join(f"0x{value:08X}" for value in duplicate_performance_programs)
            raise DependencyError(f"Duplicate Performance program numbers: {values}.")
        target_performance_programs = tuple(
            _user_performance_program(index) for index in range(len(selected))
        )
        performance_program_map = dict(
            zip(
                (item.program_number for item in selected),
                target_performance_programs,
                strict=True,
            )
        )
        usage: Counter[int] = Counter()
        for performance in selected:
            usage.update(set(performance.entry.references))
        waveform_programs = tuple(sorted(usage))
        if len(waveform_programs) > 1024:
            raise DependencyError("A classic Library supports at most 1,024 Waveforms.")
        waveform_program_map = {
            program: 0x00010001 + index for index, program in enumerate(waveform_programs)
        }

        block_map = self.library.block_map
        blockers: list[str] = []
        warnings: list[str] = list(self.library.warnings)
        waveform_indices, missing_waveform_metadata = _indices_for_programs(
            block_map.get("EWFM"), waveform_programs, "Waveform metadata", blockers
        )
        waveform_image_indices, missing_waveform_images = _indices_for_programs(
            block_map.get("EWIM"), waveform_programs, "Waveform Image/sample", blockers
        )
        for program in sorted(set(missing_waveform_metadata) | set(missing_waveform_images)):
            metadata_missing = program in missing_waveform_metadata
            image_missing = program in missing_waveform_images
            if metadata_missing and image_missing:
                missing_description = "metadata and samples"
            elif metadata_missing:
                missing_description = "metadata"
            else:
                missing_description = "samples"
            bank = (program >> 16) & 0xFFFF
            if 2 <= bank <= 9:
                blockers.append(
                    f"Missing external dependency: Waveform 0x{program:08X} points to "
                    f"Library bank {bank}, but '{self.library.source_name}' does not contain "
                    f"its {missing_description}. The Library file that "
                    "contains this Waveform is also required to create a standalone output."
                )
            else:
                blockers.append(
                    f"Invalid reference: Waveform 0x{program:08X} has no "
                    f"{missing_description} in this file."
                )

        preserved_suffixes: list[str] = []
        preserved_block_ids: set[str] = set()
        resource_counts: dict[str, int] = {}
        graph_edges: dict[str, set[str]] = {}

        for performance in selected:
            performance_node = _node("PFM", performance.program_number)
            graph_edges.setdefault(performance_node, set())
            for reference in performance.entry.references:
                graph_edges[performance_node].add(_node("WFM", reference))
                graph_edges.setdefault(_node("WFM", reference), set()).add(
                    _node("WIM", reference)
                )

        for suffix, entry_block in _entry_blocks_by_suffix(block_map).items():
            if suffix in DIRECTLY_INTERPRETED_SUFFIXES or suffix in NON_DEPENDENCY_SUFFIXES:
                continue
            is_known_potential = suffix in POTENTIAL_DEPENDENCY_SUFFIXES
            description = (
                f"Internal references to {suffix} cannot yet be interpreted safely."
            )
            if conservative:
                preserved_suffixes.append(suffix)
                preserved_block_ids.update({entry_block.chunk_id, "D" + suffix})
                resource_counts[suffix] = len(entry_block.entries)
                warnings.append(description + " All items of this type will be preserved.")
                for performance in selected:
                    source = _node("PFM", performance.program_number)
                    for entry in entry_block.entries:
                        graph_edges.setdefault(source, set()).add(
                            _node(suffix, entry.program_number)
                        )
                for entry in entry_block.entries:
                    bank = entry.program_number >> 16
                    if 2 <= bank <= 9:
                        blockers.append(
                            f"Resource {suffix} 0x{entry.program_number:08X} belongs to an "
                            "installed Library bank. Preserving it in a standalone Library "
                            "requires remapping a payload that is not yet interpreted."
                        )
            else:
                qualifier = "potential dependency" if is_known_potential else "unknown chunk"
                blockers.append(
                    f"Non-conservative mode cannot discard {suffix} ({qualifier}) without "
                    "interpreting its references."
                )

        for block in self.library.blocks:
            if not block.opaque:
                continue
            if conservative:
                preserved_block_ids.add(block.chunk_id)
                warnings.append(
                    f"Opaque chunk {block.chunk_id} will be preserved in full for safety."
                )
            else:
                blockers.append(
                    f"Opaque chunk {block.chunk_id} prevents export outside conservative mode."
                )

        data_block = block_map.get("DPFM")
        if data_block is None:
            blockers.append("DPFM block is missing.")
        else:
            for performance in selected:
                record = data_block.data_records[performance.index]
                blob = self.library.source_range(record.source_offset + 8, record.payload_size)
                try:
                    internal_references = scan_classic_waveform_references(blob)
                except (FormatError, ValueError) as exc:
                    blockers.append(
                        f"DPFM for '{performance.name}' could not be interpreted for "
                        f"safe remapping: {exc}"
                    )
                    continue
                internal_programs = {
                    reference.program_number
                    for reference in internal_references
                    if reference.bank != 0
                }
                missing_from_entry = internal_programs.difference(performance.entry.references)
                if missing_from_entry:
                    values = ", ".join(
                        f"0x{value:08X}" for value in sorted(missing_from_entry)
                    )
                    blockers.append(
                        f"DPFM for '{performance.name}' contains dependencies missing from EPFM: "
                        f"{values}."
                    )

        frozen_graph = DependencyGraph(
            {key: frozenset(values) for key, values in graph_edges.items()}
        )
        roots = tuple(_node("PFM", performance.program_number) for performance in selected)
        return DependencySelection(
            performance_indices=indices,
            performance_programs=tuple(item.program_number for item in selected),
            performance_program_map=performance_program_map,
            target_performance_programs=target_performance_programs,
            waveform_programs=waveform_programs,
            waveform_program_map=waveform_program_map,
            waveform_indices=waveform_indices,
            waveform_image_indices=waveform_image_indices,
            shared_waveforms={key: value for key, value in usage.items() if value > 1},
            preserved_suffixes=tuple(sorted(preserved_suffixes)),
            preserved_block_ids=tuple(sorted(preserved_block_ids)),
            resource_counts=resource_counts,
            graph=frozen_graph,
            closure=frozen_graph.closure(roots),
            warnings=tuple(dict.fromkeys(warnings)),
            blockers=tuple(dict.fromkeys(blockers)),
            conservative=conservative,
        )


def is_user_performance_program(program: int) -> bool:
    msb = (program >> 16) & 0xFFFF
    lsb = (program >> 8) & 0xFF
    number = program & 0xFF
    return msb == 0x003F and 0x20 <= lsb <= 0x24 and number <= 0x7F


def _user_performance_program(index: int) -> int:
    if not 0 <= index < 640:
        raise ValueError(f"Performance index outside the User bank: {index}.")
    return 0x003F2000 | ((index // 128) << 8) | (index % 128)


def _parse_program_number(value: str) -> int | None:
    stripped = value.strip()
    if not stripped.lower().startswith("0x"):
        return None
    try:
        program = int(stripped, 16)
    except ValueError as exc:
        raise DependencyError(f"Invalid program number: '{value}'.") from exc
    if not 0 <= program <= 0xFFFFFFFF:
        raise DependencyError(f"Program number outside the 32-bit range: '{value}'.")
    return program


def remap_program_numbers(
    entries: tuple[EntryRecord, ...],
    mapping: dict[int, int],
) -> tuple[EntryRecord, ...]:
    """Explicit, tested primitive for formats whose payload references are understood."""
    return tuple(
        entry.remapped(
            program_number=mapping.get(entry.program_number),
            reference_map=mapping,
        )
        for entry in entries
    )


def _indices_for_programs(
    block: Block | None,
    programs: tuple[int, ...],
    kind: str,
    blockers: list[str],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not programs:
        return (), ()
    if block is None:
        blockers.append(f"{kind} block is missing, but there are {len(programs)} reference(s).")
        return (), ()
    by_program: dict[int, int] = {}
    duplicates: set[int] = set()
    for index, entry in enumerate(block.entries):
        if entry.program_number in by_program:
            duplicates.add(entry.program_number)
        by_program[entry.program_number] = index
    for duplicate in sorted(duplicates):
        blockers.append(f"{kind} has a duplicate program number: 0x{duplicate:08X}.")
    indices: list[int] = []
    missing: list[int] = []
    for program in programs:
        matched_index = by_program.get(program)
        if matched_index is None:
            missing.append(program)
        else:
            indices.append(matched_index)
    return tuple(indices), tuple(missing)


def _entry_blocks_by_suffix(block_map: dict[str, Block]) -> dict[str, Block]:
    return {
        block.suffix: block
        for block in block_map.values()
        if block.is_entry_block and "D" + block.suffix in block_map
    }


def _node(kind: str, program: int) -> str:
    return f"{kind}:0x{program:08X}"
