from pathlib import Path

from PySide6.QtWidgets import QApplication

from yamaha_library.gui import MainWindow, performance_label
from yamaha_library.model import FileFamily
from yamaha_library.npno import NpnoParser
from yamaha_library.parser import YamahaLibraryParser

from .synthetic import make_library, make_npno


def test_performance_label_shows_original_and_compact_target() -> None:
    assert performance_label("Example", 0x003F202C) == (
        "Example  —  original ID 0x003F202C"
    )
    assert performance_label("Example", 0x003F202C, 0x003F2000) == (
        "Example  —  original 0x003F202C → new ID 0x003F2000 (User position 1)"
    )
    assert performance_label("Motif", 0x003F0805, 0x003F0800, "Voice User 1") == (
        "Motif  —  original 0x003F0805 → new ID 0x003F0800 (Voice User 1)"
    )


def test_classic_output_selector_offers_x7_and_x8() -> None:
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.library = YamahaLibraryParser().parse_bytes(make_library(), "source.X8L")
    window.output.setText("subset.X8L")

    window._configure_output_formats()

    assert window.output_format.count() == 2
    assert window._target_family() is FileFamily.X8
    window.output_format.setCurrentIndex(0)
    assert window._target_family() is FileFamily.X7
    assert window.output.text().endswith(".X7L")
    window.close()
    application.processEvents()


def test_npno_output_selector_is_fixed_to_x8(tmp_path: Path) -> None:
    application = QApplication.instance() or QApplication([])
    source = tmp_path / "piano.npno"
    source.write_bytes(make_npno())
    window = MainWindow()
    window.library = NpnoParser().parse(source)

    window._configure_output_formats()

    assert window.output_format.count() == 1
    assert window._target_family() is FileFamily.X8
    assert window._output_extension() == ".X8L"
    assert not window.output_format.isEnabled()
    window.close()
    application.processEvents()
