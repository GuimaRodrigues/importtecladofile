"""PySide6 desktop interface."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .dependencies import DependencyResolver, DependencySelection
from .diagnostics import probe_ysfc_file
from .errors import YamahaLibraryError
from .exporter import ExportResult, LibraryExporter
from .loader import SourceLibrary, load_library
from .model import FileFamily, ParsedLibrary
from .motif import MotifSelection, MotifSubsetExporter, MotifSubsetResolver
from .npno import NpnoLibrary
from .npno_exporter import NpnoResolver, NpnoSelection, prepare_exporter

FILE_FILTER = (
    "Supported instrument files (*.npno *.X0A *.X0V *.X7A *.X7U *.X7L *.X8A *.X8U *.X8L)"
)
Selection = DependencySelection | MotifSelection | NpnoSelection


def performance_label(
    name: str,
    original_program: int,
    target_program: int | None = None,
    position_label: str | None = None,
) -> str:
    if target_program is None:
        return f"{name}  —  original ID 0x{original_program:08X}"
    if position_label is None:
        bank_page = (target_program >> 8) & 0xFF
        bank_offset = target_program & 0xFF
        user_position = ((bank_page - 0x20) * 128) + bank_offset + 1
        position_label = f"User position {user_position}"
    return (
        f"{name}  —  original 0x{original_program:08X} → "
        f"new ID 0x{target_program:08X} ({position_label})"
    )


class ExportWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        library: SourceLibrary,
        selection: Selection,
        destination: Path,
        overwrite: bool,
        target_family: FileFamily | None,
    ) -> None:
        super().__init__()
        self.library = library
        self.selection = selection
        self.destination = destination
        self.overwrite = overwrite
        self.target_family = target_family

    @Slot()
    def run(self) -> None:
        try:
            if isinstance(self.selection, NpnoSelection):
                if not isinstance(self.library, NpnoLibrary):
                    raise TypeError("NPNO selection used with a YSFC library.")
                result = prepare_exporter(self.library, self.selection).export(
                    self.selection, self.destination, overwrite=self.overwrite
                )
            elif isinstance(self.selection, MotifSelection):
                if not isinstance(self.library, ParsedLibrary):
                    raise TypeError("MOTIF selection used with a non-YSFC source.")
                result = MotifSubsetExporter(self.library).export(
                    self.selection, self.destination, overwrite=self.overwrite
                )
            else:
                if not isinstance(self.library, ParsedLibrary):
                    raise TypeError("YSFC selection used with a non-YSFC source.")
                result = LibraryExporter(
                    self.library, target_family=self.target_family
                ).export(
                    self.selection, self.destination, overwrite=self.overwrite
                )
            self.completed.emit(result)
        except (YamahaLibraryError, OSError, TypeError, ValueError) as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.library: SourceLibrary | None = None
        self.selection: Selection | None = None
        self.export_thread: QThread | None = None
        self.export_worker: ExportWorker | None = None
        self.last_open_path: Path | None = None
        self.last_open_error: str | None = None
        self.setWindowTitle(f"Yamaha Library Subsetter v{__version__}")
        self.resize(920, 720)
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        file_row = QHBoxLayout()
        self.open_button = QPushButton("Open Yamaha file…")
        self.open_button.clicked.connect(self.open_file)
        self.file_label = QLabel("No file open")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnostic_button = QPushButton("Copy diagnostic")
        self.diagnostic_button.clicked.connect(self.copy_diagnostic)
        self.diagnostic_button.setEnabled(False)
        file_row.addWidget(self.open_button)
        file_row.addWidget(self.file_label, 1)
        file_row.addWidget(self.diagnostic_button)
        layout.addLayout(file_row)

        self.general_label = QLabel(
            "Select an NPNO, X0A/X0V, X7A/X7U/X7L, or X8A/X8U/X8L file."
        )
        self.general_label.setWordWrap(True)
        layout.addWidget(self.general_label)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search for a Performance or Voice by name…")
        self.search.textChanged.connect(self._apply_filter)
        self.search.setEnabled(False)
        layout.addWidget(self.search)

        self.performance_list = QListWidget()
        self.performance_list.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.performance_list, 1)

        selection_row = QHBoxLayout()
        self.select_all_button = QPushButton("Select all")
        self.clear_button = QPushButton("Clear selection")
        self.select_all_button.clicked.connect(self._select_all)
        self.clear_button.clicked.connect(self._clear_selection)
        self.conservative = QCheckBox("Conservative mode")
        self.conservative.setChecked(True)
        self.conservative.toggled.connect(self._selection_changed)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.clear_button)
        selection_row.addStretch(1)
        selection_row.addWidget(self.conservative)
        layout.addLayout(selection_row)

        self.summary = QLabel("0 items selected")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Format:"))
        self.output_format = QComboBox()
        self.output_format.setEnabled(False)
        self.output_format.currentIndexChanged.connect(self._output_format_changed)
        output_row.addWidget(self.output_format)
        self.output = QLineEdit()
        self.output.setPlaceholderText("New Library destination")
        self.output.setEnabled(False)
        self.browse_output_button = QPushButton("Choose destination…")
        self.browse_output_button.clicked.connect(self.choose_output)
        self.browse_output_button.setEnabled(False)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(self.browse_output_button)
        layout.addLayout(output_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.export_button = QPushButton("Export new Library")
        self.export_button.clicked.connect(self.export)
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)

        self.setCentralWidget(central)
        close_action = QAction("Exit", self)
        close_action.triggered.connect(self.close)
        self.menuBar().addMenu("File").addAction(close_action)

    @Slot()
    def open_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Open Yamaha file", "", FILE_FILTER)
        if not filename:
            return
        self.last_open_path = Path(filename).resolve()
        self.last_open_error = None
        try:
            library = load_library(filename)
        except YamahaLibraryError as exc:
            self.last_open_error = str(exc)
            self.diagnostic_button.setEnabled(True)
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Critical)
            dialog.setWindowTitle("Invalid file")
            dialog.setText(str(exc))
            dialog.setInformativeText(
                "Click “Copy diagnostic” in the main window and send the text to support."
            )
            dialog.exec()
            return
        self.library = library
        self.selection = None
        self.diagnostic_button.setEnabled(True)
        self.file_label.setText(str(Path(filename)))
        items = library.selectable_items
        kind = library.selectable_kind
        kind_plural = "Voices" if kind == "Voice" else "Instruments" if kind == "Instrument" else "Performances"
        if isinstance(library, NpnoLibrary):
            self.general_label.setText(
                f"Nord NPNO / {library.header.version} - {library.source_size:,} bytes - "
                f"{len(library.strokes)} strokes - checksum verified"
            )
        else:
            self.general_label.setText(
                f"{library.family.value} / YSFC {library.header.version} - "
                f"{library.source_size:,} bytes - {len(items)} {kind_plural} - "
                f"chunks: {', '.join(library.chunk_ids)}"
            )
        self.performance_list.blockSignals(True)
        self.performance_list.clear()
        for performance in items:
            item = QListWidgetItem(performance_label(performance.name, performance.program_number))
            item.setData(Qt.ItemDataRole.UserRole, performance.index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.performance_list.addItem(item)
        self.performance_list.blockSignals(False)
        self.search.setEnabled(True)
        self.output.setEnabled(True)
        self.browse_output_button.setEnabled(True)
        self._configure_output_formats()
        suggested = Path(filename).with_name(
            Path(filename).stem + "-subset" + self._output_extension()
        )
        self.output.setText(str(suggested))
        self._selection_changed()

    @Slot()
    def copy_diagnostic(self) -> None:
        if self.last_open_path is None:
            return
        payload: dict[str, object] = {
            "application": {
                "name": "Yamaha Library Subsetter",
                "version": __version__,
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "file_probe": (
                {"name": self.last_open_path.name, "format": "NPNO"}
                if self.last_open_path.suffix.casefold() == ".npno"
                else probe_ysfc_file(self.last_open_path)
            ),
        }
        if self.last_open_error is not None:
            payload["open_error"] = self.last_open_error
        elif self.library is not None:
            if isinstance(self.library, NpnoLibrary):
                payload["parsed"] = {
                    "family": "NPNO",
                    "input_extension": ".NPNO",
                    "version": self.library.header.version,
                    "name": self.library.name,
                    "strokes": len(self.library.strokes),
                    "warnings": list(self.library.warnings),
                }
            else:
                payload["parsed"] = {
                    "family": self.library.family.value,
                    "input_extension": self.library.input_extension,
                    "ysfc_version": self.library.header.version,
                    "selectable_kind": self.library.selectable_kind,
                    "selectable_count": len(self.library.selectable_items),
                    "chunks": [
                        {"id": block.chunk_id, "count": block.count}
                        for block in self.library.blocks
                    ],
                    "warnings": list(self.library.warnings),
                }
            selection_payload = self._diagnostic_selection_payload()
            if selection_payload is not None:
                payload["selection"] = selection_payload
            target_family = self._target_family()
            payload["output_format"] = {
                "family": (
                    target_family.value
                    if target_family is not None
                    else ("NPNO" if isinstance(self.library, NpnoLibrary) else self.library.family.value)
                ),
                "extension": self._output_extension(),
                "cross_family_conversion": bool(
                    target_family is not None
                    and not isinstance(self.library, NpnoLibrary)
                    and target_family is not self.library.family
                ),
            }
        QApplication.clipboard().setText(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        self.statusBar().showMessage("Diagnostic copied to the clipboard.", 6000)

    def _diagnostic_selection_payload(self) -> dict[str, object] | None:
        if self.library is None or self.selection is None:
            return None
        if isinstance(self.selection, NpnoSelection):
            return {
                "items": [self.library.selectable_items[0].name],
                "can_export": self.selection.can_export,
                "warnings": list(self.selection.warnings),
                "blockers": list(self.selection.blockers),
            }
        if isinstance(self.selection, MotifSelection):
            indices = self.selection.item_indices
            mapping = self.selection.program_map
            warnings = self.selection.warnings
            blockers = self.selection.blockers
        else:
            indices = self.selection.performance_indices
            mapping = self.selection.performance_program_map
            warnings = self.selection.warnings
            blockers = self.selection.blockers
        items = self.library.selectable_items
        return {
            "items": [
                {
                    "name": items[index].name,
                    "source_program": f"0x{items[index].program_number:08X}",
                    "target_program": f"0x{mapping[items[index].program_number]:08X}",
                }
                for index in indices
            ],
            "can_export": self.selection.can_export,
            "warnings": list(warnings),
            "blockers": list(blockers),
        }

    @Slot(str)
    def _apply_filter(self, query: str) -> None:
        needle = query.casefold().strip()
        for row in range(self.performance_list.count()):
            item = self.performance_list.item(row)
            item.setHidden(bool(needle and needle not in item.text().casefold()))

    @Slot()
    def _select_all(self) -> None:
        self.performance_list.blockSignals(True)
        for row in range(self.performance_list.count()):
            self.performance_list.item(row).setCheckState(Qt.CheckState.Checked)
        self.performance_list.blockSignals(False)
        self._selection_changed()

    @Slot()
    def _clear_selection(self) -> None:
        self.performance_list.blockSignals(True)
        for row in range(self.performance_list.count()):
            self.performance_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self.performance_list.blockSignals(False)
        self._selection_changed()

    @Slot()
    def _selection_changed(self) -> None:
        if self.library is None:
            self.summary.setText("0 items selected")
            self.export_button.setEnabled(False)
            return
        indices = [
            int(self.performance_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.performance_list.count())
            if self.performance_list.item(row).checkState() == Qt.CheckState.Checked
        ]
        if not indices:
            self.selection = None
            self._refresh_performance_labels(None)
            self.summary.setText("0 items selected")
            self.export_button.setEnabled(False)
            return
        selection: Selection
        try:
            if isinstance(self.library, NpnoLibrary):
                npno_selection = NpnoResolver(self.library).resolve(indices)
                estimate = prepare_exporter(self.library, npno_selection).estimate_size(
                    npno_selection
                )
                selection = npno_selection
            elif self.library.family is FileFamily.X0:
                motif_selection = MotifSubsetResolver(self.library).resolve(
                    indices, conservative=self.conservative.isChecked()
                )
                estimate = MotifSubsetExporter(self.library).estimate_size(motif_selection)
                selection = motif_selection
            else:
                dependency_selection = DependencyResolver(self.library).resolve(
                    indices, conservative=self.conservative.isChecked()
                )
                estimate = LibraryExporter(
                    self.library, target_family=self._target_family()
                ).estimate_size(dependency_selection)
                selection = dependency_selection
        except YamahaLibraryError as exc:
            self.selection = None
            self._refresh_performance_labels(None)
            self.summary.setText(f"Error resolving dependencies: {exc}")
            self.export_button.setEnabled(False)
            return
        self.selection = selection
        self._refresh_performance_labels(selection)
        if isinstance(selection, NpnoSelection):
            assert isinstance(self.library, NpnoLibrary)
            status = "ready to export" if selection.can_export else "export blocked"
            details = (
                f"1 Nord instrument - {len(self.library.attack_strokes)} attack strokes - "
                f"{len(self.library.release_strokes)} release strokes - "
                f"{len(selection.waveform_plans)} MODX Waveforms - "
                f"estimated size {estimate:,} bytes - {status}"
            )
            if selection.blockers:
                details += "\n" + "\n".join(f"Blocker: {value}" for value in selection.blockers)
            elif selection.warnings:
                details += "\n" + "\n".join(f"Warning: {value}" for value in selection.warnings)
            self.summary.setText(details)
            self.export_button.setEnabled(selection.can_export)
            return
        if isinstance(selection, MotifSelection):
            self._show_motif_summary(selection, estimate)
            return
        resources = sum(selection.resource_counts.values())
        assert isinstance(self.library, ParsedLibrary)
        status = "ready to export" if selection.can_export else "export blocked"
        details = (
            f"{selection.performance_count} Performances • {selection.waveform_count} Waveforms • "
            f"{selection.sample_count} sample payloads • "
            f"{len(selection.shared_waveforms)} shared Waveforms • "
            f"{resources} conservatively preserved resources • "
            f"estimated size {estimate:,} bytes • {status}"
        )
        first_target = selection.target_performance_programs[0]
        last_target = selection.target_performance_programs[-1]
        details += (
            f"\nNew IDs: 0x{first_target:08X} to 0x{last_target:08X} "
            f"(User positions 1 to {selection.performance_count})."
        )
        target_family = self._target_family()
        if target_family is not None:
            details += f"\nOutput format: {target_family.value}L."
            if target_family is not self.library.family:
                details += " Experimental X7/X8 conversion; validate the file on the keyboard."
        if selection.blockers:
            details += "\n" + "\n".join(f"Blocker: {value}" for value in selection.blockers)
        elif selection.warnings:
            details += "\n" + "\n".join(f"Warning: {value}" for value in selection.warnings)
        self.summary.setText(details)
        self.export_button.setEnabled(selection.can_export)

    def _show_motif_summary(self, selection: MotifSelection, estimate: int) -> None:
        status = "ready to export" if selection.can_export else "export blocked"
        preserved = sum(selection.preserved_counts.values())
        plural = "Voices" if selection.item_kind == "Voice" else "Performances"
        details = (
            f"{selection.item_count} {plural} • {preserved} preserved resources • "
            f"estimated size {estimate:,} bytes • {status}"
        )
        details += (
            f"\nNew IDs compacted from {selection.position_labels[0]}; "
            "experimental MOTIF XS format, requires testing on the keyboard."
        )
        if selection.blockers:
            details += "\n" + "\n".join(f"Blocker: {value}" for value in selection.blockers)
        elif selection.warnings:
            details += "\n" + "\n".join(f"Warning: {value}" for value in selection.warnings)
        self.summary.setText(details)
        self.export_button.setEnabled(selection.can_export)

    def _refresh_performance_labels(self, selection: Selection | None) -> None:
        if self.library is None:
            return
        if isinstance(selection, NpnoSelection):
            mapping = {}
            position_map = {}
        elif isinstance(selection, MotifSelection):
            mapping = selection.program_map
            position_map = dict(
                zip(selection.source_programs, selection.position_labels, strict=True)
            )
        elif selection is not None:
            mapping = selection.performance_program_map
            position_map = {}
        else:
            mapping = {}
            position_map = {}
        items = self.library.selectable_items
        self.performance_list.blockSignals(True)
        try:
            for row in range(self.performance_list.count()):
                item = self.performance_list.item(row)
                index = int(item.data(Qt.ItemDataRole.UserRole))
                performance = items[index]
                item.setText(
                    performance_label(
                        performance.name,
                        performance.program_number,
                        mapping.get(performance.program_number),
                        position_map.get(performance.program_number),
                    )
                )
        finally:
            self.performance_list.blockSignals(False)

    @Slot()
    def choose_output(self) -> None:
        if self.library is None:
            return
        extension = self._output_extension()
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save new Library",
            self.output.text(),
            f"Yamaha file (*{extension})",
        )
        if filename:
            path = Path(filename)
            if path.suffix.upper() != extension:
                path = path.with_suffix(extension)
            self.output.setText(str(path))

    @Slot()
    def export(self) -> None:
        if self.library is None or self.selection is None:
            return
        destination = Path(self.output.text()).resolve()
        extension = self._output_extension()
        if destination.suffix.upper() != extension:
            destination = destination.with_suffix(extension)
            self.output.setText(str(destination))
        overwrite = False
        report_path = destination.with_suffix(destination.suffix + ".json")
        if destination.exists() or report_path.exists():
            answer = QMessageBox.question(
                self,
                "Replace files?",
                "The Library or JSON report already exists. Do you want to replace them?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        self._set_busy(True)
        thread = QThread(self)
        worker = ExportWorker(
            self.library,
            self.selection,
            destination,
            overwrite,
            self._target_family(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._export_completed)
        worker.failed.connect(self._export_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.export_thread = thread
        self.export_worker = worker
        thread.start()

    @Slot(object)
    def _export_completed(self, result: ExportResult) -> None:
        self._set_busy(False)
        self.export_thread = None
        self.export_worker = None
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Export complete")
        dialog.setText(
            f"Library created at:\n{result.output_path}\n\nReport:\n{result.report_path}"
            "\n\nThe selected items were renumbered from the first compatible slot."
        )
        dialog.setDetailedText(json.dumps(result.report, ensure_ascii=False, indent=2))
        dialog.exec()

    @Slot(str)
    def _export_failed(self, message: str) -> None:
        self._set_busy(False)
        self.export_thread = None
        self.export_worker = None
        QMessageBox.critical(self, "Export failed", message)

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.open_button.setEnabled(not busy)
        self.export_button.setEnabled(not busy and bool(self.selection and self.selection.can_export))
        self.browse_output_button.setEnabled(not busy and self.library is not None)
        self.output_format.setEnabled(
            not busy
            and bool(
                self.library
                and not isinstance(self.library, NpnoLibrary)
                and self.library.family is not FileFamily.X0
            )
        )

    def _configure_output_formats(self) -> None:
        if self.library is None:
            return
        self.output_format.blockSignals(True)
        try:
            self.output_format.clear()
            if isinstance(self.library, NpnoLibrary):
                self.output_format.addItem("MODX / MODX+ (.X8L)", FileFamily.X8.value)
                self.output_format.setEnabled(False)
                return
            if self.library.family is FileFamily.X0:
                self.output_format.addItem(
                    f"Original format ({self.library.output_extension})", FileFamily.X0.value
                )
                self.output_format.setEnabled(False)
                return
            self.output_format.addItem("MONTAGE (.X7L)", FileFamily.X7.value)
            self.output_format.addItem("MODX / MODX+ (.X8L)", FileFamily.X8.value)
            source_index = 0 if self.library.family is FileFamily.X7 else 1
            self.output_format.setCurrentIndex(source_index)
            self.output_format.setEnabled(True)
        finally:
            self.output_format.blockSignals(False)

    def _target_family(self) -> FileFamily | None:
        if self.library is None:
            return None
        if isinstance(self.library, NpnoLibrary):
            return FileFamily.X8
        if self.library.family is FileFamily.X0:
            return None
        value = self.output_format.currentData()
        if value not in {FileFamily.X7.value, FileFamily.X8.value}:
            return self.library.family
        return FileFamily(str(value))

    def _output_extension(self) -> str:
        if self.library is None:
            return ".X8L"
        target_family = self._target_family()
        if target_family is None:
            return self.library.output_extension
        return target_family.library_extension

    @Slot(int)
    def _output_format_changed(self, _index: int) -> None:
        if self.library is None:
            return
        current = Path(self.output.text()) if self.output.text() else None
        if current is not None:
            self.output.setText(str(current.with_suffix(self._output_extension())))
        self._selection_changed()


def main() -> int:
    application = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
