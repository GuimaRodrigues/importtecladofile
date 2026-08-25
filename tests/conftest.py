from __future__ import annotations

from pathlib import Path

import pytest

from .synthetic import make_library


@pytest.fixture
def synthetic_bytes() -> bytes:
    return make_library()


@pytest.fixture
def synthetic_file(tmp_path: Path, synthetic_bytes: bytes) -> Path:
    path = tmp_path / "source.X8L"
    path.write_bytes(synthetic_bytes)
    return path

