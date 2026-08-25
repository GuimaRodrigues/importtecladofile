# Supported structures

## Fully supported structure

- 64-byte header and `YAMAHA-YSFC` signature;
- versions `1.0.0`/`1.0.1` (MOTIF XS X0), `4.0.1`–`4.0.5` (X7), and `5.0.1` (X8), checked against the extension;
- big-endian `chunk ID + offset` catalogue;
- empty Library Info and occupied-slot descriptions;
- block headers, counts, and bounds;
- `Entr` and `Data` records, including the size/offset relationship;
- the alternate MOTIF XS `Entr` layout and absence of Library Info in X0;
- known IDs: `PFM`, `WFM`, `WIM`, `ARP`, `MSQ`, `LST`, `CRV`, `MTN`, `PTN`, `PAT`, `SNG`,
  `SYS`, `FVT`, `PCH`, `SPG`, and `SOM`;
- unknown IDs, recognized as opaque blocks and preserved in conservative mode;
- names, titles, flags, timestamps, program numbers, and the Waveform list in `EPFM`.

## Interpreted dependencies

- Performance → Waveform through the additional `EPFM` program number;
- Waveform → `DWFM` metadata and `DWIM` Waveform Image/sample payload through the `EWFM`/`EWIM`
  program number;
- sharing and transitive closure of those edges;
- Waveform references in AWM2 and Drum Elements in classic `DPFM`;
- Library 1–8 → User compaction for Performances, `EPFM`, `DPFM`, `EWFM`, and `EWIM`;
- X7↔X8 container conversion, using YSFC `4.0.5` for new X7 output and `5.0.1` for new X8 output;
- migration of X7 `4.0.1`/`4.0.2` `Entr` records to the modern layout with a sort ID;
- missing or duplicate references, which are blockers.

## Conservative preservation

Until their references inside `DPFM` are decoded, all present `ARP`, `MSQ`, `CRV`, `MTN`, `PTN`,
`SPG`, `SOM`, and unknown items are preserved. This prioritizes integrity but can retain unused
resources, so a minimal subset is not promised in these cases. `LST`, `PAT`, `SNG`, `SYS`, `FVT`,
and `PCH` are not Performance dependencies and are omitted to avoid broken global references.

For X0V, selection operates on `EVCE/DVCE`, compacts normal Voices and Drum Voices separately, and
preserves every `WFM/WIM` pair. For X0A, selection operates on `EPFM/DPFM`, compacts Performance IDs
from zero, and preserves Voices, Waveforms, and all other pairs in full. This strategy avoids
rewriting still-opaque internal MOTIF XS payloads, but it does not produce a minimal subset and still
requires validation in MODX/MONTAGE.

## Not yet interpreted

- `DPFM` fields beyond Part framing and Waveform references; references to Arpeggios, Curves,
  Motion Sequences, Microtunings, Auditions, and Smart Morph remain opaque;
- individual Keybanks and boundaries between samples within each `DWIM`; the application accurately
  counts Waveform Image payloads but does not claim an internal PCM sample count;
- the internal layout of `DWFM`, `DARP`, `DCRV`, `DMSQ`, `DMTN`, `DPTN`, `DSPG`, and `DSOM`;
- Yamaha's exact rule for the two totals displayed in Library Info;
- file versions other than `1.0.0`, `1.0.1`, `4.0.1`–`4.0.5`, and `5.0.1`;
- internal MOTIF XS Voice/Performance references, which is why X0 resources are preserved together;
- remapping Library 1–8 items other than Performances/Waveforms;
- X0A/X0V to X7L/X8L conversion; these formats remain under the separate MOTIF XS backend.

The remapping limitation is a deliberate blocker. Performances/Waveforms are also rewritten in
`DPFM`; other types will be enabled only when they have equivalent validation.

## Available evidence

Synthetic fixtures cover every container and export invariant. During development, a private
486,069,640-byte X8A was analyzed read-only: version 5.0.1, 598 Performances, 40 WFM/WIM pairs, and
14 chunks. All 598 DPFM blobs were scanned; a full export compacted 37 Waveforms, reopened with the
exact 598 names, and passed dependency resolution again. Hardware validation is still mandatory.
That file is not part of the project.
