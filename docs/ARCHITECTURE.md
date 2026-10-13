# Architecture

The project keeps binary format handling, safety decisions, and the interface separate:

```text
X0/X7/X8 or NPNO file
    │
    ▼
parser ──► immutable model ──► graph/resolver ──► export plan
                                                     │
                                                     ▼
                                           atomic writer + JSON
        CLI ◄─────────────────────────────────────────┤
        GUI ◄─────────────────────────────────────────┘
```

## Layers

`binary.py` contains only bounds-checked big-endian reads. `parser.py` validates the YSFC container,
Library Info, catalogue, and records. It uses `mmap` and stores offsets to `Data` payloads instead of
copying hundreds of megabytes into memory.

`model.py` defines immutable dataclasses. Small, rewritable `Entr` records remain in memory; `Data`
records point to ranges in the source. The source file is always opened read-only.

`dependencies.py` builds a directed graph. The known edges are Performance → Waveform → Waveform
Image. `classic_dpfm.py` walks the classic framing and locates `wave_bank` and the little-endian
number in AWM2/Drum Elements. Resources with unknown internal layouts receive conservative edges
from the Performance to every item of that type. A selection carries warnings and blockers, never
only a silent Boolean.

`motif.py` is a separate backend for MOTIF XS. It selects `PFM` in X0A or `VCE` in X0V, recalculates
the catalogue and offsets, and preserves every potentially dependent opaque pair. This separation
prevents experimental X0 rules from affecting the X7/X8 resolver and writer.

`npno.py` is the separate Nord CBIN/CNSP parser and PCM decoder. It retains stroke offsets instead
of decoded audio. `npno_exporter.py` decodes and resamples one stroke at a time, maps attack and
key-off release strokes to at most eight AWM2 elements, streams `DWIM`, then reopens the generated
X8L and resolves its dependencies before publishing it.

`exporter.py` builds a plan for each `Exxx`/`Dxxx` pair, recalculates item and catalogue offsets,
copies samples in 4 MiB chunks, writes in the destination directory, and calls `fsync`. Before
`replace`, the parser reopens the temporary file and checks the exact Performance list. A JSON
report containing the SHA-256, resources, and warnings accompanies the output. When the target
differs from the source family, the writer changes the YSFC version. For X7 `4.0.1`/`4.0.2`, it also
inserts sort IDs into every `Entr` before recalculating offsets. Converted X7 uses `4.0.5`; converted
X8 uses `5.0.1`.

`cli.py` and `gui.py` are adapters. The GUI runs exports in a `QThread`; no binary-format rules live
in the interface.

## Safety invariants

1. The source is never opened for writing and can never be the output path.
2. Every parsed offset must be within the file.
3. `Entr.item_size` must equal `Data.payload_size`.
4. Every `Entr.data_offset` must point to the start of the corresponding `Data` record.
5. E/D pairs must exist and have the same count.
6. Missing explicit dependencies block export.
7. Unknown semantics are preserved or block export.
8. Performance/Waveform program numbers are compacted into User space; `EPFM` and `DPFM`
   references are rewritten and reopened. Other opaque payloads remain blocked when they require
   remapping.

## Future extensions

New decoders must produce exact reference edges and positions, with A/B tests against real fixtures
that can legally be shared. Only then can a type leave the conservative-preservation list. The
`DPFM` remapper is the first payload decoder; Arpeggios, Curves, Motion Sequences, and Smart Morph
still need equivalents.
