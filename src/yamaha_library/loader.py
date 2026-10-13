"""Dispatch source files to the YSFC or NPNO parser."""

from __future__ import annotations

from pathlib import Path

from .model import ParsedLibrary
from .npno import NpnoLibrary, NpnoParser
from .parser import YamahaLibraryParser

type SourceLibrary = ParsedLibrary | NpnoLibrary


def load_library(path: str | Path) -> SourceLibrary:
    source = Path(path)
    if source.suffix.casefold() == ".npno":
        return NpnoParser().parse(source)
    return YamahaLibraryParser().parse(source)
