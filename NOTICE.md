# Technical references and attribution

The high-level structure was studied from Chris Webb's public
[`arachsys/montage`](https://github.com/arachsys/montage) repository, particularly
`docs/files/structure.md` and the demonstration scripts under `files/`. That documentation also
credits Derek Cook of x.factory Librarians for research into Library Info and Performance
attributes.

At the time it was reviewed for this project, the reference repository contained neither a
`LICENSE` file nor an explicit license grant in its README. No source code from those scripts was
therefore copied or adapted. This implementation was written independently using only factual
format descriptions and its own validation. If a license is published later, this decision can be
reassessed while retaining any required notices.

Yamaha, MONTAGE, and MODX are trademarks of their respective owners. This project is neither
affiliated with nor endorsed by Yamaha.

## Additional cross-validation

The classic `DPFM` framing and functional positions of AWM2/Drum references were also checked
against the MIT-licensed [`YSFC Forge`](https://github.com/YSFCforge/ysfc-forge) project by Johan
Adolfsson and contributors. YSFC Forge itself attributes part of its classic support to the
LGPL-3.0-licensed [`ConvertWithMoss`](https://github.com/git-moss/ConvertWithMoss) project by Jürgen
Moßgraber and contributors.

No code from those projects was incorporated. `classic_dpfm.py` is a minimal, independent
implementation of interoperability facts, with bounds checking and comparison against the 598 real
blobs available during development.

The MOTIF XS generation's header and `Entr` layout were also checked against the LGPL-3.0
ConvertWithMoss reader and independently validated against the X0A/X0V examples supplied for this
project. No Java code was incorporated; `motif.py` implements only conservative structural
selection and preserves opaque internal payloads without attempting to translate them.

The structural difference between MONTAGE YSFC 4.0.1/4.0.2 (without an `Entr` sort ID) and
4.0.3–4.0.5 was likewise validated through independent fixtures and the reference reader. The
writer preserves the legacy-version header field instead of inventing missing IDs.
