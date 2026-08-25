# Validation on a real Yamaha keyboard

Reopening the output with the parser proves structural consistency, not firmware or audio
compatibility. Run this procedure separately on MONTAGE (X7) and MODX/MODX+ (X8), using a current
backup and trusted USB media.

For X0A/X0V, the initial goal is import into MODX/MONTAGE, not a complete MOTIF XS restore. Keep
conservative mode enabled and test the `*-subset-test.X0A/X0V` files first.

## Before you begin

1. Save a complete keyboard backup and confirm that it can be read.
2. Copy the source and record its SHA-256 (`Get-FileHash file.X8L -Algorithm SHA256`).
3. Start with a small real fixture created on the keyboard itself: two Performances, one exclusive
   Waveform, one shared Waveform, one User Arpeggio, and one User Curve.
4. Do not begin with an irreplaceable Library. Importing can consume flash memory and time.

## For each output

1. Run `yamaha-library inspect OUTPUT.X8L --json` and archive the result.
2. Check the source SHA-256 again; it must be identical.
3. Load the output into an empty slot using the instrument's official procedure.
4. Confirm that only the selected names appear in Category Search.
5. Open each Performance and test every Part, Element/Operator, key range, and velocity range.
6. Test sustained notes, loops, release, velocity variation, and sample playback through the end.
7. Test Arpeggio, Motion Sequence, User Curve, Microtuning, Audition, and Smart Morph when used.
8. Confirm that the same shared Waveform works in every dependent Performance.
9. Restart the instrument, reopen the Performances, and repeat a short check.
10. Remove the Library through the keyboard and confirm that no external content was changed.
11. Attach the model, firmware version, result, symptoms, and relevant photos/logs to the JSON report.

## Minimum pending test matrix

| Family | Source | Case | Real file required |
|---|---|---|---|
| X7 | X7U → X7L | User, no samples | Yes |
| X7 | X7L → X7L | Exclusive/shared Waveforms | Yes |
| X7 | X7A → X7L | Standalone User bank | Yes |
| X7 | X7L 4.0.1/4.0.2 → X7L | Legacy layout without an `Entr` sort ID | Yes |
| X8 | X8U → X8L | User, no samples | Yes |
| X8 | X8L → X8L | Exclusive/shared Waveforms | Yes |
| X8 | X8A → X8L | Standalone User bank | Yes |
| X7→X8 | X7L 4.0.5 → X8L | Modern conversion | Yes |
| X7→X8 | X7L 4.0.1/4.0.2 → X8L | `Entr` layout migration | Yes |
| X8→X7 | X8L → X7L 4.0.5 | Modern conversion | Yes |
| X7/X8 | Any | User Arpeggio + User Curve | Yes |
| X7/X8 | Any | Motion Sequence/Microtuning/Audition/Smart Morph | Yes |
| X0 | X0V → X0V | One normal Voice with its own Waveforms | Yes |
| X0 | X0V → X0V | One Drum Voice | Yes |
| X0 | X0A → X0A | One Performance, loaded into MODX as a Performance | Yes |
| X0 | X0A → X0A | One Performance that depends on a User Voice/Waveform | Yes |

Output containing Performances/Waveforms from Library banks 1–8 requires special attention: the
structural remapping passed round-trip and cross-comparison tests but still needs confirmation
through playback on MODX. Other User resources originating in Library banks remain blocked when
their payload cannot be safely rewritten.

## Acceptance criteria

A family can be marked as validated only after every applicable row passes on at least two supported
firmware versions, with no broken references, missing samples, source changes, or unexpected extra
resources. Record the version and hashes; do not generalize the result to untested firmware.
