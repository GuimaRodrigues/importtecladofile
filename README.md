# Yamaha Library Subsetter

Python desktop application and CLI for inspecting Yamaha MOTIF XS, MONTAGE/MODX, and Nord Piano
Library files and creating conservative Library subsets or converting NPNO instruments to X8L.
The program never modifies the source file: it recalculates
the catalogue, offsets, and counts, writes through a temporary file, and reopens the output before
publishing it.

> Compatibility status: the YSFC container and explicit Waveform dependencies are supported and
> tested. Classic `DPFM` Waveform remapping is supported, but the proprietary contents of
> Arpeggios, Curves, and parts of `DWFM` are not fully documented. Conservative mode preserves
> resources with uncertain dependencies or blocks uninterpreted payloads. Hardware compatibility
> is not claimed until the [validation procedure](VALIDATION.md) has been completed.

## Features

- Opens `.npno`, `.X0A`, `.X0V`, `.X7A`, `.X7U`, `.X7L`, `.X8A`, `.X8U`, and `.X8L` files.
- Validates NPNO CBIN/CNSP structure and checksum, decodes its compressed PCM, resamples it to
  44.1 kHz, and creates an X8L with an AWM2 Performance for MODX/MODX+.
- Recognizes MONTAGE X7 YSFC revisions `4.0.1` through `4.0.5`, including the `Entr` layout change.
- Validates the signature, version, catalogue, Library Info, bounds, `Entr`/`Data` pairs, offsets, and sizes.
- Handles large sample payloads with `mmap` and streaming copies.
- Lists and searches Performances, with individual selection, select all, and clear actions.
- Lists Voices in X0V files and Performances in X0A files.
- Builds a dependency graph and identifies shared Waveforms.
- Converts Performances and Waveforms from Library banks 1–8 to compact User IDs.
- Rewrites and verifies internal references in AWM2 and Drum Parts in `DPFM`.
- Preserves every `DWFM`/`DWIM` payload required by the selected Waveforms.
- Conservatively preserves Arpeggios, Curves, and other potentially referenced resources.
- Creates `.X7L` output for X7 and `.X8L` output for X8, plus a JSON report.
- Lets you choose `.X7L` or `.X8L` for an X7/X8 source, converting the YSFC version and migrating
  the legacy X7 record layout when required.
- Creates conservative `.X0V`/`.X0A` subsets while preserving opaque MOTIF XS resources.
- Includes a CLI, a PySide6 GUI, synthetic fixtures, and round-trip tests.

## Installation on Windows

Python 3.12 or newer is required. In PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,build]"
```

To install only the application, run `python -m pip install -e .`.

## Desktop interface

```powershell
yamaha-library-gui
```

Open a file, select the Performances or Voices, review warnings and blockers, choose a destination,
and export. Conservative mode is enabled by default. Sample copying runs outside the UI thread.

For X7/X8 files, the **Format** field offers `MONTAGE (.X7L)` and `MODX / MODX+ (.X8L)`. The source
format is selected by default. Cross-family conversion is experimental and must be validated on the
keyboard. It is normally unnecessary because MODX loads X7L files, and MONTAGE with OS 3.5 or newer
loads X8L files. X0A/X0V files remain in their original MOTIF XS format.

The window title shows the running version. **Copy diagnostic** produces a small JSON document with
the header, catalogue, chunks, selection, and current error. It does not copy samples or musical
payloads and can be shared when troubleshooting a problem on another computer.

If a Performance points to Waveforms from another Library that are not physically present in the
open file, standalone export is blocked and the external bank is reported. The program never
silently replaces these references because doing so would alter or remove parts of the sound.

X0 support is experimental and intended for import into MODX/MONTAGE. All Waveforms are preserved
in X0V output; all Voices and other resources are preserved in X0A output to retain internal
references. Output must be tested on hardware before production use.

NPNO conversion is also experimental. Attack samples and key-off release samples become AWM2
elements, including mono/stereo Keybanks and velocity ranges. Nord pedal-resonance samples are
omitted because AWM2 has no equivalent trigger; velocity response and per-key tuning between
sampled roots are approximations.
Always audition the result on the target keyboard. The source `.npno` is read-only and remains
unchanged.

## CLI

```powershell
yamaha-library inspect file.X8L
yamaha-library list file.X8L
yamaha-library dependencies file.X8L --performance "Name"
yamaha-library subset file.X8L `
  --performance "Performance A" `
  --performance "Performance B" `
  --target-format X7L `
  --output MyLibrary.X7L

yamaha-library inspect "Astoria Grand Lrg 6.1.npno"
yamaha-library subset "Astoria Grand Lrg 6.1.npno" `
  --performance "Astoria Grand" `
  --output "Astoria Grand Lrg 6.1.X8L"
```

When using `--target-format`, the `--output` extension must match the selected target.

Use `--json` with `inspect`, `list`, and `dependencies`. Use `--force` with `subset` to replace an
existing destination. The CLI rejects duplicate names because silently choosing one would be
unsafe; the GUI lets you select a specific occurrence by its displayed program number.

## Development and testing

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

Public fixtures are generated in memory by `tests/synthetic.py`. Real files must be placed in
`tests/fixtures/private`; this directory's contents and all Yamaha file formats are ignored by Git.

## Windows executables

```powershell
.\scripts\build.ps1
```

The script uses PyInstaller to produce `dist\YamahaLibrarySubsetter\YamahaLibrarySubsetter.exe` and
`dist\yamaha-library\yamaha-library.exe`.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Supported structures and limitations](docs/SUPPORTED_STRUCTURES.md)
- [Validation on real hardware](VALIDATION.md)
- [Technical references and attribution](NOTICE.md)
