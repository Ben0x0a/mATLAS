"""Runtime-loaded PySide6 GUI for Model Atlas."""
from __future__ import annotations

import html
import logging
import os
import signal
import sys
import tempfile
import traceback
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

import yaml
from PySide6.QtCore import QFile, QObject, QRectF, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
)

from gui import accessibility
from gui.widgets.drag_drop_line_edit import DragDropLineEdit
from gui.theme import build_stylesheet, configure_application_font
from launcher.profiles import PROFILE_SUFFIX, build_profile_preset_folder, load_profile, save_profile
from model_atlas.pipeline import ProcessResult, process
from model_atlas.presets.spec_loader import load_preset_specs

log = logging.getLogger("model_atlas.gui")

DESKTOP_ICON_ASSET = "matlas_transformer_desktop_icon.svg"
TRANSITION_ASSETS = {
    2: "matlas_transition_01_standby.svg",
    3: "matlas_transition_02_activate.svg",
    4: "matlas_transition_03_compact.svg",
    5: "matlas_transition_04_reform.svg",
    6: "matlas_transition_05_bed_form.svg",
    7: "matlas_transition_06_complete.svg",
}


def _asset_path(name: str) -> Path:
    return Path(files("gui").joinpath("assets", name))


@dataclass(frozen=True)
class PresetItem:
    name: str
    path: Path


class _LogBridge(QObject):
    record = Signal(str, str)


class _GuiLogHandler(logging.Handler):
    def __init__(self, bridge: _LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - logging must not crash the GUI
            self.handleError(record)
            return
        colour = {
            logging.DEBUG: "#667085",
            logging.INFO: "#2563eb",
            logging.WARNING: "#b45309",
            logging.ERROR: "#b91c1c",
            logging.CRITICAL: "#7f1d1d",
        }.get(record.levelno, "#111827")
        self._bridge.record.emit(colour, message)


class _TaskWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self._fn())
        except Exception:  # noqa: BLE001 - surface all failures in the GUI
            self.failed.emit(traceback.format_exc())


class MainController(QObject):
    """Controller for the simple single-window GUI."""

    def __init__(self) -> None:
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app.setStyle("Fusion")
            configure_application_font(app)
        self.window = self._load_ui()
        self._threads: list[QThread] = []
        self._workers: list[_TaskWorker] = []
        self._presets: list[PresetItem] = []
        self._last_output_folder: Path | None = None
        self._status_text = "Idle"
        self._log_bridge: _LogBridge | None = None
        self._log_handler: _GuiLogHandler | None = None

        self._bind_widgets()
        self._configure_ui_defaults()
        self._wire_signals()
        self._install_log_handler()
        self._apply_log_level()
        if app is not None:
            app.aboutToQuit.connect(self._wait_for_threads)
        QTimer.singleShot(0, self._load_presets_async)

    def _load_ui(self) -> QMainWindow:
        loader = QUiLoader()
        loader.registerCustomWidget(DragDropLineEdit)
        ui_path = files("gui").joinpath("main_window.ui")
        qfile = QFile(str(ui_path))
        if not qfile.open(QFile.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"Could not open GUI layout: {ui_path}")
        try:
            widget = loader.load(qfile)
        finally:
            qfile.close()
        if not isinstance(widget, QMainWindow):
            raise TypeError("main_window.ui root must be QMainWindow")
        return widget

    def _bind_widgets(self) -> None:
        def child(cls: type, name: str):
            widget = self.window.findChild(cls, name)
            if widget is None:
                raise RuntimeError(f"Missing widget {name!r} in main_window.ui")
            return widget

        self.source_edit: QLineEdit = child(QLineEdit, "sourceEdit")
        self.presets_path_edit: QLineEdit = child(QLineEdit, "presetsPathEdit")
        self.output_edit: QLineEdit = child(QLineEdit, "outputEdit")
        self.entity_edit: QLineEdit = child(QLineEdit, "entityEdit")
        self.linked_entity_edit: QLineEdit = child(QLineEdit, "linkedEntityEdit")
        self.preset_search_edit: QLineEdit = child(QLineEdit, "presetSearchEdit")
        self.available_preset_list: QListWidget = child(QListWidget, "availablePresetList")
        self.selected_preset_list: QListWidget = child(QListWidget, "selectedPresetList")
        self.log_edit: QTextEdit = child(QTextEdit, "logEdit")
        self.status_label: QLabel = child(QLabel, "statusLabel")
        self.preset_status_label: QLabel = child(QLabel, "presetStatusLabel")
        self.single_preset_notice_label: QLabel = child(QLabel, "singlePresetNoticeLabel")
        self.output_label: QLabel = child(QLabel, "outputLabel")

        self.source_file_button: QPushButton = child(QPushButton, "sourceFileButton")
        self.source_folder_button: QPushButton = child(QPushButton, "sourceFolderButton")
        self.presets_folder_button: QPushButton = child(QPushButton, "presetsFolderButton")
        self.presets_file_button: QPushButton = child(QPushButton, "presetsFileButton")
        self.output_button: QPushButton = child(QPushButton, "outputButton")
        self.add_preset_button: QPushButton = child(QPushButton, "addPresetButton")
        self.remove_preset_button: QPushButton = child(QPushButton, "removePresetButton")
        self.run_button: QPushButton = child(QPushButton, "runButton")
        self.open_output_folder_button: QPushButton = child(QPushButton, "openOutputFolderButton")
        self.clear_form_button: QPushButton = child(QPushButton, "clearFormButton")
        self.clear_log_button: QPushButton = child(QPushButton, "clearLogButton")
        self.load_profile_button: QPushButton = child(QPushButton, "loadProfileButton")
        self.save_profile_button: QPushButton = child(QPushButton, "saveProfileButton")
        self.reload_presets_button: QPushButton = child(QPushButton, "reloadPresetsButton")
        self.auto_preset_check: QCheckBox = child(QCheckBox, "autoPresetCheck")
        self.merge_outputs_check: QCheckBox = child(QCheckBox, "mergeOutputsCheck")
        self.dump_full_ufdr_check: QCheckBox = child(QCheckBox, "dumpFullUfdrCheck")

        self.traceability_combo: QComboBox = child(QComboBox, "traceabilityCombo")
        self.log_level_combo: QComboBox = child(QComboBox, "logLevelCombo")

    def _configure_ui_defaults(self) -> None:
        default_presets = Path.cwd() / "presets"
        self.presets_path_edit.setText(str(default_presets))
        self.log_edit.setAcceptRichText(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self.window.resize(1000, 860)
        self.window.setWindowTitle("Model ATLAS Transformer")
        self.window.setWindowIcon(QIcon(str(_asset_path(DESKTOP_ICON_ASSET))))
        self.window.setStyleSheet(build_stylesheet())
        self._configure_header_assets()
        self._hide_unreleased_options()
        self.single_preset_notice_label.setVisible(False)
        accessibility.configure_status_label(self)
        self._stabilize_form_labels()
        self._stabilize_preset_mode_row()
        self._sync_output_mode()
        accessibility.configure_accessibility(self)

    def _wire_signals(self) -> None:
        self.source_file_button.clicked.connect(self._browse_source_file)
        self.source_folder_button.clicked.connect(self._browse_source_folder)
        self.presets_folder_button.clicked.connect(self._browse_presets_folder)
        self.presets_file_button.clicked.connect(self._browse_presets_file)
        self.output_button.clicked.connect(self._browse_output)
        self.presets_path_edit.editingFinished.connect(self._load_presets_async)
        self.preset_search_edit.textChanged.connect(self._render_available_presets)
        self.add_preset_button.clicked.connect(self._add_selected_presets)
        self.remove_preset_button.clicked.connect(self._remove_selected_presets)
        self.available_preset_list.itemDoubleClicked.connect(lambda _item: self._add_selected_presets())
        self.selected_preset_list.itemDoubleClicked.connect(lambda _item: self._remove_selected_presets())
        self.run_button.clicked.connect(self._run_process)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.clear_form_button.clicked.connect(self._clear_form)
        self.clear_log_button.clicked.connect(self.log_edit.clear)
        self.load_profile_button.clicked.connect(self._load_profile)
        self.save_profile_button.clicked.connect(self._save_profile)
        self.reload_presets_button.clicked.connect(self._load_presets_async)
        self.auto_preset_check.toggled.connect(lambda _checked: self._sync_single_preset_notice())
        self.log_level_combo.currentTextChanged.connect(self._apply_log_level)
        self.merge_outputs_check.toggled.connect(lambda _checked: self._sync_output_mode(clear_output=True))

    def _stabilize_form_labels(self) -> None:
        label_names = (
            "sourceLabel",
            "identityLabel",
            "presetsPathLabel",
            "outputModeLabel",
            "outputLabel",
            "traceabilityLabel",
        )
        for name in label_names:
            label = self.window.findChild(QLabel, name)
            if label is not None:
                label.setMinimumWidth(125)

    def _hide_unreleased_options(self) -> None:
        self.dump_full_ufdr_check.setVisible(False)
        if (label := self.window.findChild(QLabel, "dumpFullUfdrLabel")) is not None:
            label.setVisible(False)

    def eventFilter(self, watched: QObject, event: object) -> bool:
        accessibility.handle_status_resize(self, watched, event)
        return super().eventFilter(watched, event)

    def _set_status(self, text: str) -> None:
        accessibility.set_status(self, text)

    def _configure_header_assets(self) -> None:
        for number in range(2, 8):
            label = self.window.findChild(QLabel, f"headerImage{number}")
            if label is None:
                continue
            label.setFixedSize(80, 60)
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            pixmap = self._render_svg_pixmap(_asset_path(TRANSITION_ASSETS[number]), label)
            if not pixmap.isNull():
                label.setPixmap(pixmap)

    def _render_svg_pixmap(self, path: Path, label: QLabel) -> QPixmap:
        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            return QPixmap()

        target = label.size()
        default_size = renderer.defaultSize()
        if default_size.isValid() and default_size.width() > 0 and default_size.height() > 0:
            aspect = default_size.width() / default_size.height()
            if target.width() / target.height() > aspect:
                render_height = target.height()
                render_width = int(render_height * aspect)
            else:
                render_width = target.width()
                render_height = int(render_width / aspect)
        else:
            render_width = target.width()
            render_height = target.height()

        dpr = self._device_pixel_ratio()
        pixmap = QPixmap(max(1, int(target.width() * dpr)), max(1, int(target.height() * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        x = (target.width() - render_width) / 2
        y = (target.height() - render_height) / 2
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        renderer.render(painter, QRectF(x, y, render_width, render_height))
        painter.end()
        return pixmap

    def _device_pixel_ratio(self) -> float:
        app = QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        return float(screen.devicePixelRatio()) if screen is not None else 1.0

    def _stabilize_preset_mode_row(self) -> None:
        label = self.window.findChild(QLabel, "presetModeLabel")
        if label is not None:
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            label.setFixedWidth(label.sizeHint().width())
        self.auto_preset_check.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.auto_preset_check.setFixedWidth(self.auto_preset_check.sizeHint().width())

    def _install_log_handler(self) -> None:
        bridge = _LogBridge(self)
        bridge.record.connect(self._append_log)
        handler = _GuiLogHandler(bridge)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self._log_bridge = bridge
        self._log_handler = handler

    def _apply_log_level(self) -> None:
        level = getattr(logging, self.log_level_combo.currentText(), logging.INFO)
        logging.getLogger("model_atlas").setLevel(level)
        logging.getLogger("model_atlas.gui").setLevel(level)

    @Slot(str, str)
    def _append_log(self, colour: str, message: str) -> None:
        self.log_edit.append(f'<span style="color:{colour}">{html.escape(message)}</span>')

    def _run_thread(
        self,
        fn: Callable[[], Any],
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ) -> None:
        thread = QThread(self)
        worker = _TaskWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: self._threads.remove(thread) if thread in self._threads else None)
        thread.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self._workers.append(worker)
        thread.start()

    def _wait_for_threads(self) -> None:
        for thread in list(self._threads):
            if thread.isRunning():
                thread.wait()

    def _browse_source_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Choose source file",
            self.source_edit.text() or str(Path.cwd()),
            "Supported sources (*.csv *.xlsx *.xlsm *.xltx *.xltm *.db *.sqlite *.sqlite3 *.zip);;All files (*)",
        )
        if path:
            self.source_edit.setText(path)

    def _browse_source_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self.window,
            "Choose source folder",
            self.source_edit.text() or str(Path.cwd()),
        )
        if path:
            self.source_edit.setText(path)

    def _browse_presets_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self.window,
            "Choose presets folder",
            self.presets_path_edit.text() or str(Path.cwd() / "presets"),
        )
        if path:
            self.presets_path_edit.setText(path)
            self._load_presets_async()

    def _browse_presets_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Choose preset YAML",
            self.presets_path_edit.text() or str(Path.cwd() / "presets"),
            "YAML presets (*.yaml *.yml);;All files (*)",
        )
        if path:
            self.presets_path_edit.setText(path)
            self._load_presets_async()

    def _browse_output(self) -> None:
        if self.merge_outputs_check.isChecked():
            path, _ = QFileDialog.getSaveFileName(
                self.window,
                "Choose merged output CSV",
                self.output_edit.text() or str(Path.cwd() / "out" / "merged.csv"),
                "CSV (*.csv);;All files (*)",
            )
        else:
            path = QFileDialog.getExistingDirectory(
                self.window,
                "Choose output folder",
                self.output_edit.text() or str(Path.cwd() / "out"),
            )
        if path:
            self.output_edit.setText(path)

    def _sync_output_mode(self, *, clear_output: bool = False) -> None:
        if clear_output:
            self.output_edit.clear()
        if self.merge_outputs_check.isChecked():
            self.output_label.setText("Output CSV")
            self.output_edit.setPlaceholderText("merged.csv")
            self.output_button.setText("Save as...")
        else:
            self.output_label.setText("Output folder")
            self.output_edit.setPlaceholderText("folder for per-preset CSV output")
            self.output_button.setText("Folder...")
        accessibility.update_output_accessibility(self)

    def _load_presets_async(self) -> None:
        presets_path = self._preset_root()
        if not presets_path:
            return
        self.preset_status_label.setText("Loading presets...")
        self.preset_status_label.setAccessibleDescription("Loading presets.")
        self._run_thread(
            lambda: [
                PresetItem(spec.name, spec.path)
                for spec in load_preset_specs(presets_path)
            ],
            self._on_presets_loaded,
            self._on_presets_failed,
        )

    def _preset_root(self) -> Path | None:
        text = self.presets_path_edit.text().strip()
        return Path(text).expanduser() if text else None

    def _on_presets_loaded(self, presets: list[PresetItem]) -> None:
        self._presets = sorted(presets, key=lambda item: item.name.casefold())
        self.preset_status_label.setText(f"{len(self._presets)} preset(s) loaded")
        self.preset_status_label.setAccessibleDescription(f"{len(self._presets)} presets loaded.")
        self._render_available_presets()
        self._sync_single_preset_notice()
        log.info("Loaded %d preset(s) for GUI selection", len(self._presets))

    def _on_presets_failed(self, tb: str) -> None:
        self._presets = []
        self.available_preset_list.clear()
        self.preset_status_label.setText("Preset load failed")
        self.preset_status_label.setAccessibleDescription("Preset loading failed.")
        self._sync_single_preset_notice()
        log.error("Preset loading failed:\n%s", tb)

    def _render_available_presets(self) -> None:
        selected = set(self._selected_preset_paths())
        needle = self.preset_search_edit.text().strip().casefold()
        self.available_preset_list.clear()
        for preset in self._presets:
            if str(preset.path) in selected:
                continue
            if needle and needle not in preset.name.casefold():
                continue
            self._add_list_item(self.available_preset_list, preset)

    def _add_list_item(self, list_widget: QListWidget, preset: PresetItem) -> None:
        item = QListWidgetItem(preset.name)
        item.setToolTip(str(preset.path))
        item.setData(Qt.ItemDataRole.UserRole, str(preset.path))
        list_widget.addItem(item)

    def _add_selected_presets(self) -> None:
        paths = set(self._selected_preset_paths())
        by_path = {str(preset.path): preset for preset in self._presets}
        for item in self.available_preset_list.selectedItems():
            path = item.data(Qt.ItemDataRole.UserRole)
            if path in paths:
                continue
            preset = by_path.get(path)
            if preset is None:
                continue
            self._add_list_item(self.selected_preset_list, preset)
            paths.add(path)
        if self.selected_preset_list.count():
            self.auto_preset_check.setChecked(False)
        self._render_available_presets()
        self._sync_single_preset_notice()

    def _remove_selected_presets(self) -> None:
        for item in self.selected_preset_list.selectedItems():
            row = self.selected_preset_list.row(item)
            self.selected_preset_list.takeItem(row)
        self._render_available_presets()
        self._sync_single_preset_notice()

    def _selected_preset_paths(self) -> list[str]:
        return [
            str(self.selected_preset_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.selected_preset_list.count())
        ]

    def _load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Load mATLAS profile",
            str(Path.cwd()),
            f"mATLAS profile (*{PROFILE_SUFFIX});;All files (*)",
        )
        if not path:
            return
        try:
            preset_paths = load_profile(Path(path))
        except Exception as exc:  # noqa: BLE001 - show user-facing validation failure
            self._show_error(str(exc))
            return
        self.selected_preset_list.clear()
        for preset_path in preset_paths:
            self._add_list_item(self.selected_preset_list, self._preset_item_for_path(preset_path))
        self.auto_preset_check.setChecked(False)
        self._render_available_presets()
        self._sync_single_preset_notice()
        log.info("Loaded profile %s with %d preset(s)", path, len(preset_paths))

    def _save_profile(self) -> None:
        selected_paths = [Path(path) for path in self._selected_preset_paths()]
        if not selected_paths:
            self._show_error("Select at least one preset before saving a profile.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Save mATLAS profile",
            str(Path.cwd() / f"profile{PROFILE_SUFFIX}"),
            f"mATLAS profile (*{PROFILE_SUFFIX});;All files (*)",
        )
        if not path:
            return
        try:
            written = save_profile(Path(path), selected_paths)
        except Exception as exc:  # noqa: BLE001 - show user-facing write failure
            self._show_error(str(exc))
            return
        log.info("Saved profile %s with %d preset(s)", written, len(selected_paths))

    def _preset_item_for_path(self, path: Path) -> PresetItem:
        path = Path(path)
        for preset in self._presets:
            if preset.path.resolve() == path.resolve():
                return preset
        try:
            specs = load_preset_specs(path)
            if specs:
                return PresetItem(specs[0].name, specs[0].path)
        except Exception:
            pass
        return PresetItem(path.stem, path)

    def _single_preset_for_selector_skip(self, selected_mode: bool, selected_paths: list[Path]) -> Path | None:
        if selected_mode:
            return selected_paths[0] if len(selected_paths) == 1 else None
        return self._presets[0].path if len(self._presets) == 1 else None

    def _sync_single_preset_notice(self) -> None:
        selected_mode = not self.auto_preset_check.isChecked()
        selected_paths = [Path(path) for path in self._selected_preset_paths()]
        visible = self._single_preset_for_selector_skip(selected_mode, selected_paths) is not None
        self.single_preset_notice_label.setVisible(visible)
        self.single_preset_notice_label.setAccessibleDescription(
            "One preset will be applied without filename selector filtering." if visible else ""
        )

    def _preset_without_selector_filters(self, preset_path: Path, target_dir: Path) -> Path:
        raw = yaml.safe_load(preset_path.read_text(encoding="utf-8")) or {}
        selectors = raw.get("selectors")
        if isinstance(selectors, list):
            raw["selectors"] = [
                {"source_type": selector["source_type"]}
                for selector in selectors
                if isinstance(selector, dict) and "source_type" in selector
            ] or selectors
        target = target_dir / preset_path.name
        target.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return target

    def _run_process(self) -> None:
        source = self.source_edit.text().strip()
        output = self.output_edit.text().strip()
        preset_root = self._preset_root()
        if not source:
            self._show_error("Choose a source file or folder.")
            return
        if preset_root is None:
            self._show_error("Choose a preset folder or YAML file.")
            return
        if not output:
            if self.merge_outputs_check.isChecked():
                self._show_error("Choose an output CSV path.")
            else:
                self._show_error("Choose an output folder.")
            return
        entity = self.entity_edit.text().strip() or None
        linked_entity = self.linked_entity_edit.text().strip()
        if not linked_entity:
            # Linked entity is mandatory (matches the CLI's required --linked-entity):
            # every output row must be attributable to a case subject.
            self._show_error("Enter a linked entity (required).")
            return
        output_path = Path(output).expanduser()
        merge_outputs = self.merge_outputs_check.isChecked()
        selected_mode = not self.auto_preset_check.isChecked()
        selected_paths = [Path(path) for path in self._selected_preset_paths()]
        if selected_mode and not selected_paths:
            self._show_error("Selected-presets mode needs at least one preset.")
            return
        single_preset = self._single_preset_for_selector_skip(selected_mode, selected_paths)

        self._set_running(True)
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        traceability_format = self.traceability_combo.currentText()
        self._apply_log_level()

        def task() -> ProcessResult:
            if single_preset is not None:
                with tempfile.TemporaryDirectory(prefix="matlas-single-preset-") as tmp:
                    relaxed_preset = self._preset_without_selector_filters(single_preset, Path(tmp))
                    return process(
                        Path(source),
                        relaxed_preset,
                        output_path,
                        traceability_format=traceability_format,
                        merge=merge_outputs,
                        entity=entity,
                        linked_entity=linked_entity,
                    )
            if not selected_mode:
                return process(
                    Path(source),
                    preset_root,
                    output_path,
                    traceability_format=traceability_format,
                    merge=merge_outputs,
                    entity=entity,
                    linked_entity=linked_entity,
                )
            with tempfile.TemporaryDirectory(prefix="matlas-presets-") as tmp:
                tmp_path = build_profile_preset_folder(selected_paths, Path(tmp))
                return process(
                    Path(source),
                    tmp_path,
                    output_path,
                    traceability_format=traceability_format,
                    merge=merge_outputs,
                    entity=entity,
                    linked_entity=linked_entity,
                )

        self._run_thread(task, self._on_process_done, self._on_process_failed)

    def _on_process_done(self, result: ProcessResult) -> None:
        self._set_running(False)
        outputs = [result.output_csv] if result.output_csv is not None else list(result.output_csvs)
        outputs = [path for path in outputs if path is not None]
        if not outputs:
            self._set_status("Completed: no rows produced")
            log.warning("No rows produced: no discovered source matched a preset.")
            return
        self._last_output_folder = outputs[0].parent
        self.open_output_folder_button.setEnabled(True)
        counts = result.row_counts
        summary = (
            f"Completed: sources={counts.get('sources', 0)} "
            f"matched={counts.get('matched', 0)} rows={counts.get('rows', 0)} "
            f"ranked={counts.get('ranked', 0)}"
        )
        self._set_status(summary)
        log.info(summary)
        for csv in outputs:
            log.info("CSV: %s", csv)
        if result.output_traceability is not None:
            log.info("Traceability: %s", result.output_traceability)
        if result.output_warnings is not None:
            log.info("Warnings report: %s", result.output_warnings)

    def _on_process_failed(self, tb: str) -> None:
        self._set_running(False)
        last = tb.strip().splitlines()[-1] if tb.strip() else "Processing failed"
        self._set_status("Failed")
        log.error("Processing failed:\n%s", tb)
        self._show_error(last)

    def _set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.run_button.setText("Running..." if running else "Run")
        self._set_status("Running..." if running else "Idle")

    def _open_output_folder(self) -> None:
        if self._last_output_folder is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_folder)))

    def _clear_form(self) -> None:
        self.source_edit.clear()
        self.output_edit.clear()
        self.entity_edit.clear()
        self.linked_entity_edit.clear()
        self.presets_path_edit.setText(str(Path.cwd() / "presets"))
        self.preset_search_edit.clear()
        self.selected_preset_list.clear()
        self.traceability_combo.setCurrentText("readable")
        self.log_level_combo.setCurrentText("INFO")
        self.merge_outputs_check.setChecked(True)
        self._sync_output_mode()
        self.auto_preset_check.setChecked(True)
        self._sync_single_preset_notice()
        self._set_status("Idle")
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        self._render_available_presets()
        self._load_presets_async()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self.window, "Model Atlas", message)


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1] + (argv or []))
    app.setStyle("Fusion")
    configure_application_font(app)
    _install_sigint_handler(app)
    controller = MainController()
    controller.window.show()
    return int(app.exec())


def _install_sigint_handler(app: object) -> None:
    """Make terminal Ctrl+C terminate the GUI process immediately.

    Qt's C++ event loop never yields to the Python interpreter, so a SIGINT
    handler installed in Python alone never runs while ``app.exec()`` blocks. A
    periodic no-op QTimer wakes the interpreter often enough to let pending
    signals be delivered.
    """
    from PySide6.QtCore import QTimer

    def _handle_sigint(signum: int, _frame: object) -> None:
        try:
            sys.stderr.write("\nInterrupted by Ctrl+C; exiting.\n")
            sys.stderr.flush()
        finally:
            os._exit(128 + signum)

    signal.signal(signal.SIGINT, _handle_sigint)

    timer = QTimer()
    timer.start(200)
    timer.timeout.connect(lambda: None)
    # Keep a reference on the app so the timer is not garbage-collected.
    app._sigint_timer = timer
