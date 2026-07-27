"""Runtime-loaded PySide6 GUI controller for Model Atlas.

Defines:    MainController, the sole controller for the single-window GUI. It builds the
            window from main_window.ui, wires widget signals, and dispatches pipeline
            runs onto a background thread.
Used by:    launcher.gui (the GUI entry point constructs and shows MainController.window).
Depends on: gui.worker (TaskRunner / ProgressBridge / install_gui_log_handler),
            gui.header_assets (window icon + header images), gui.accessibility,
            gui.completion, gui.theme, gui.widgets.drag_drop_line_edit, model_atlas.app.profiles,
            and the model_atlas package app.pipeline/app.config/export/presets APIs.
"""
from __future__ import annotations

import html
import logging
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QFile, QObject, QStringListModel, QTimer, QUrl, Qt, Slot
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QWidget,
)

from gui import accessibility, header_assets
from gui.case_state import format_case_summary, read_case_counts
from gui.completion import param_completions
from gui.db_edit_controller import DbEditController
from gui.spatiotemporal_controller import SpatiotemporalController
from gui.widgets.drag_drop_line_edit import DragDropLineEdit
from gui.theme import build_stylesheet, configure_application_font
from gui.worker import ProgressBridge, TaskRunner, install_gui_log_handler
from model_atlas.app.profiles import PROFILE_SUFFIX, build_profile_preset_folder, load_profile, save_profile
from model_atlas.app.config import get_settings
from model_atlas.export.registry import available_exporters, load_builtin_exporters
from model_atlas.ingest.writer import is_matlas_run_db
from model_atlas.app.pipeline import ProcessResult, process
from model_atlas.presets.spec_loader import load_preset_specs

if TYPE_CHECKING:
    from ufdr_parser.dump import DumpSummary

log = logging.getLogger("model_atlas.gui")

# The centre header title swaps between these two modes via the "Dump full UFDR"
# checkbox: unchecked = the mATLAS transform pipeline, checked = the standalone UFDR
# parser. Consumed by _sync_ufdr_mode (gui.app).
APP_TITLE_DEFAULT = "mATLAS Transformer"
APP_TITLE_UFDR = "UFDR Parser"

# The window title's stem; the active case name is appended after a run so the window
# always shows which case is open. Consumed by _update_window_title (gui.app).
WINDOW_TITLE_BASE = "Model ATLAS Transformer"


def _should_show_single_preset_notice(
    selected_mode: bool,
    selected_paths: list[Path],
    loaded_preset_count: int,
    source_path: str,
) -> bool:
    """Return True only when the pipeline's force-preset mode will actually trigger.

    The "one preset applied without filename selector filtering" notice is truthful
    only when force-preset mode fires (model_atlas.app.pipeline): a single preset is in play
    AND the source is a single ordinary file whose format is not an archive. A single
    preset is "in play" when exactly one preset is selected (selected mode) or exactly
    one preset is loaded (auto mode). We approximate "not an archive" as "not .zip" here
    rather than re-implementing magic-byte detection; the pipeline does the authoritative
    check, so a false positive here is at worst a stale notice, never wrong processing.
    """
    single_preset_in_play = (
        len(selected_paths) == 1 if selected_mode else loaded_preset_count == 1
    )
    if not single_preset_in_play:
        return False
    text = source_path.strip()
    if not text:
        return False
    path = Path(text).expanduser()
    if not path.is_file():
        return False
    return path.suffix.lower() != ".zip"


@dataclass(frozen=True)
class PresetItem:
    name: str
    path: Path


class MainController(QObject):
    """Controller for the simple single-window GUI."""

    def __init__(self) -> None:
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app.setStyle("Fusion")
            configure_application_font(app)
        self.window = self._load_ui()
        self._runner = TaskRunner(self)
        self._presets: list[PresetItem] = []
        self._last_output_folder: Path | None = None
        # The case the GUI is working in: set to the case DB after a run, then bound to the
        # Database-Edit and Spatiotemporal tabs so every tab operates on the same case.
        self._active_case_db: Path | None = None
        self._status_text = "Idle"
        self._log_handler: logging.Handler | None = None
        self._current_phase = ""
        self._anim_dots = 0

        self._bind_widgets()
        self._configure_ui_defaults()
        self._wire_signals()
        self._attach_panels()
        self._log_handler = install_gui_log_handler(self, self._append_log)
        self._apply_log_level()
        # Progress reporting: the pipeline calls a plain callback per phase; the bridge turns each
        # call into a queued signal so the animated status updates safely from the worker thread.
        self._progress_bridge = ProgressBridge(self)
        self._progress_bridge.phase.connect(self._on_phase)
        self._run_timer = QTimer(self)
        self._run_timer.setInterval(350)
        self._run_timer.timeout.connect(self._tick_running_animation)
        if app is not None:
            app.aboutToQuit.connect(self._runner.wait_all)
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

        # Tab shell: the Transform form lives unchanged inside the first tab; the other two
        # tabs are placeholders for the Database Edit and Spatiotemporal Analysis panels.
        # All other bindings below still resolve because findChild searches recursively.
        self.tab_widget: QTabWidget = child(QTabWidget, "mainTabWidget")
        self.transform_tab: QWidget = child(QWidget, "transformTab")
        self.database_edit_tab: QWidget = child(QWidget, "databaseEditTab")
        self.spatiotemporal_tab: QWidget = child(QWidget, "spatiotemporalTab")

        self.source_edit: QLineEdit = child(QLineEdit, "sourceEdit")
        self.presets_path_edit: QLineEdit = child(QLineEdit, "presetsPathEdit")
        self.output_edit: QLineEdit = child(QLineEdit, "outputEdit")
        self.case_name_edit: QLineEdit = child(QLineEdit, "caseNameEdit")
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
        self.app_title_label: QLabel = child(QLabel, "appTitleLabel")
        self.case_summary_label: QLabel = child(QLabel, "caseSummaryLabel")
        self.new_case_button: QPushButton = child(QPushButton, "newCaseButton")
        self.open_case_button: QPushButton = child(QPushButton, "openCaseButton")

        self.source_file_button: QPushButton = child(QPushButton, "sourceFileButton")
        self.source_folder_button: QPushButton = child(QPushButton, "sourceFolderButton")
        self.presets_folder_button: QPushButton = child(QPushButton, "presetsFolderButton")
        self.presets_file_button: QPushButton = child(QPushButton, "presetsFileButton")
        self.output_button: QPushButton = child(QPushButton, "outputButton")
        self.add_preset_button: QPushButton = child(QPushButton, "addPresetButton")
        self.remove_preset_button: QPushButton = child(QPushButton, "removePresetButton")
        self.add_all_presets_button: QPushButton = child(QPushButton, "addAllPresetsButton")
        self.remove_all_presets_button: QPushButton = child(QPushButton, "removeAllPresetsButton")
        self.run_button: QPushButton = child(QPushButton, "runButton")
        self.open_output_folder_button: QPushButton = child(QPushButton, "openOutputFolderButton")
        self.open_error_report_button: QPushButton = child(QPushButton, "openErrorReportButton")
        self.clear_form_button: QPushButton = child(QPushButton, "clearFormButton")
        self.clear_log_button: QPushButton = child(QPushButton, "clearLogButton")
        self.load_profile_button: QPushButton = child(QPushButton, "loadProfileButton")
        self.save_profile_button: QPushButton = child(QPushButton, "saveProfileButton")
        self.reload_presets_button: QPushButton = child(QPushButton, "reloadPresetsButton")
        self.auto_preset_check: QCheckBox = child(QCheckBox, "autoPresetCheck")
        self.dump_full_ufdr_check: QCheckBox = child(QCheckBox, "dumpFullUfdrCheck")
        self.include_source_columns_check: QCheckBox = child(QCheckBox, "includeSourceColumnsCheck")

        self.traceability_combo: QComboBox = child(QComboBox, "traceabilityCombo")
        self.log_level_combo: QComboBox = child(QComboBox, "logLevelCombo")
        self.additional_export_button: QToolButton = child(QToolButton, "additionalExportButton")

    def _configure_ui_defaults(self) -> None:
        default_presets = Path.cwd() / "presets"
        self.presets_path_edit.setText(str(default_presets))
        self.log_edit.setAcceptRichText(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self.window.resize(1000, 860)
        self.window.setWindowTitle(WINDOW_TITLE_BASE)
        self.window.setWindowIcon(QIcon(str(header_assets.asset_path(header_assets.DESKTOP_ICON_ASSET))))
        self.window.setStyleSheet(build_stylesheet())
        header_assets.configure_header_assets(self.window)
        self._configure_ufdr_toggle()
        self.single_preset_notice_label.setVisible(False)
        accessibility.configure_status_label(self)
        self._stabilize_form_labels()
        self._stabilize_preset_mode_row()
        self._sync_output_mode()
        self._configure_entity_completion()
        self._configure_exports()
        accessibility.configure_accessibility(self)

    def _attach_panels(self) -> None:
        """Build the two MVC panels inside their tab placeholders.

        Each controller owns its whole panel and takes only the tab QWidget; all logic
        lives in the model layer (edit_api / analyze.concomitance) — the shell just
        constructs them. The edit panel reuses the shared TaskRunner so the app keeps a
        single lifecycle-managed worker (waited on aboutToQuit)."""
        # Drop the placeholder labels first: they were scaffolding for exactly these
        # panels, and each controller reuses the tab's existing layout, so a surviving
        # placeholder would render above the real panel.
        for tab, placeholder_name in (
            (self.database_edit_tab, "databaseEditPlaceholderLabel"),
            (self.spatiotemporal_tab, "spatiotemporalPlaceholderLabel"),
        ):
            placeholder = tab.findChild(QLabel, placeholder_name)
            if placeholder is not None:
                placeholder.setParent(None)
                placeholder.deleteLater()
        self._db_edit_controller = DbEditController(self.database_edit_tab, runner=self._runner)
        self._spatiotemporal_controller = SpatiotemporalController(self.spatiotemporal_tab)

    def _configure_exports(self) -> None:
        """Populate the 'Additional export' button with a checkable menu, one item per registered
        exporter (built-ins + any drop-in plugins). Built with Qt's QMenu/QAction — no custom
        widget. The selection is read at run time and passed to process()."""
        load_builtin_exporters()
        menu = QMenu(self.additional_export_button)
        self._export_actions: dict[str, QAction] = {}
        for spec in available_exporters():
            action = menu.addAction(spec.label)
            action.setCheckable(True)
            action.setToolTip(spec.description)
            # The primary CSV is now an exporter like any other; check it by default so an
            # untouched run still writes the flat CSV (unchecking it opts out).
            action.setChecked(spec.name == "csv")
            action.toggled.connect(lambda _checked: self._sync_export_button_text())
            self._export_actions[spec.name] = action
        self.additional_export_button.setMenu(menu)
        self.additional_export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._sync_export_button_text()

    def _selected_exports(self) -> list[str]:
        return [name for name, action in self._export_actions.items() if action.isChecked()]

    def _sync_export_button_text(self) -> None:
        """Reflect the selection on the button: 'None' when nothing is chosen, the single export's
        label when one is chosen, or 'first label (+N)' when several are."""
        selected = self._selected_exports()
        if not selected:
            self.additional_export_button.setText("None")
        elif len(selected) == 1:
            self.additional_export_button.setText(self._export_actions[selected[0]].text())
        else:
            first_label = self._export_actions[selected[0]].text()
            self.additional_export_button.setText(f"{first_label} (+{len(selected) - 1})")

    def _configure_entity_completion(self) -> None:
        """Give the entity / linked_entity fields a completer: the call-form keywords, plus
        ``field(<column>)`` per column when the input is a single CSV (see gui.completion)."""
        self._entity_completion_model = QStringListModel(self.window)
        for edit in (self.entity_edit, self.linked_entity_edit):
            completer = QCompleter(self.window)
            completer.setModel(self._entity_completion_model)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            edit.setCompleter(completer)
        self._refresh_entity_completion()

    def _refresh_entity_completion(self) -> None:
        """Rebuild the entity/linked_entity completion list for the current input path."""
        self._entity_completion_model.setStringList(param_completions(self.source_edit.text().strip()))

    def _on_source_changed(self, _text: str) -> None:
        """Source path changed: refresh entity completion and re-evaluate the single-preset
        notice, whose truthfulness depends on whether the source is a single non-archive file."""
        self._refresh_entity_completion()
        self._sync_single_preset_notice()

    def _wire_signals(self) -> None:
        self.source_file_button.clicked.connect(self._browse_source_file)
        self.source_folder_button.clicked.connect(self._browse_source_folder)
        self.presets_folder_button.clicked.connect(self._browse_presets_folder)
        self.presets_file_button.clicked.connect(self._browse_presets_file)
        self.output_button.clicked.connect(self._browse_output)
        self.source_edit.textChanged.connect(self._on_source_changed)
        self.presets_path_edit.editingFinished.connect(self._load_presets_async)
        self.preset_search_edit.textChanged.connect(self._render_available_presets)
        self.add_preset_button.clicked.connect(self._add_selected_presets)
        self.remove_preset_button.clicked.connect(self._remove_selected_presets)
        self.add_all_presets_button.clicked.connect(self._add_all_presets)
        self.remove_all_presets_button.clicked.connect(self._remove_all_presets)
        self.available_preset_list.itemDoubleClicked.connect(lambda _item: self._add_selected_presets())
        self.selected_preset_list.itemDoubleClicked.connect(lambda _item: self._remove_selected_presets())
        self.run_button.clicked.connect(self._run_process)
        self.new_case_button.clicked.connect(self._new_case)
        self.open_case_button.clicked.connect(self._open_case)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.open_error_report_button.clicked.connect(self._open_error_reports)
        self.clear_form_button.clicked.connect(self._clear_form)
        self.clear_log_button.clicked.connect(self.log_edit.clear)
        self.load_profile_button.clicked.connect(self._load_profile)
        self.save_profile_button.clicked.connect(self._save_profile)
        self.reload_presets_button.clicked.connect(self._load_presets_async)
        self.auto_preset_check.toggled.connect(lambda _checked: self._sync_single_preset_notice())
        self.log_level_combo.currentTextChanged.connect(self._apply_log_level)
        self.dump_full_ufdr_check.toggled.connect(lambda _checked: self._sync_ufdr_mode())

    def _stabilize_form_labels(self) -> None:
        label_names = (
            "sourceLabel",
            "identityLabel",
            "presetsPathLabel",
            "outputModeLabel",
            "outputLabel",
            "optionsLabel",
        )
        for name in label_names:
            label = self.window.findChild(QLabel, name)
            if label is not None:
                label.setMinimumWidth(125)

    def _configure_ufdr_toggle(self) -> None:
        """Show the UFDR-parser toggle and reflect its state in the header title.

        When checked, Run invokes the standalone UFDR parser (dump every Cellebrite model to
        CSV) instead of the transform pipeline — see ``_run_ufdr_dump``.
        """
        self.dump_full_ufdr_check.setVisible(True)
        self.dump_full_ufdr_check.setEnabled(True)
        if (label := self.window.findChild(QLabel, "dumpFullUfdrLabel")) is not None:
            label.setVisible(True)
        self._sync_ufdr_mode()

    def _sync_ufdr_mode(self) -> None:
        """Swap the centre header title between the transform and UFDR-parser modes."""
        title = APP_TITLE_UFDR if self.dump_full_ufdr_check.isChecked() else APP_TITLE_DEFAULT
        self.app_title_label.setText(title)

    def eventFilter(self, watched: QObject, event: object) -> bool:
        accessibility.handle_status_resize(self, watched, event)
        return super().eventFilter(watched, event)

    def _set_status(self, text: str) -> None:
        accessibility.set_status(self, text)

    def _stabilize_preset_mode_row(self) -> None:
        label = self.window.findChild(QLabel, "presetModeLabel")
        if label is not None:
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            label.setFixedWidth(label.sizeHint().width())
        self.auto_preset_check.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.auto_preset_check.setFixedWidth(self.auto_preset_check.sizeHint().width())

    def _apply_log_level(self) -> None:
        level = getattr(logging, self.log_level_combo.currentText(), logging.INFO)
        logging.getLogger("model_atlas").setLevel(level)
        logging.getLogger("model_atlas.gui").setLevel(level)

    @Slot(str, str)
    def _append_log(self, colour: str, message: str) -> None:
        self.log_edit.append(f'<span style="color:{colour}">{html.escape(message)}</span>')

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
        # --output is a folder: the case database is written inside it as <name>.matlas.sqlite,
        # with every other export beside it (one file per format). Always pick a directory.
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
        # The output is a folder; the case database is written inside it, with every other
        # export (one file per format) beside it.
        self.output_label.setText("Output folder")
        self.output_button.setText("Folder...")
        self.output_edit.setPlaceholderText("folder (the case database is written inside)")
        accessibility.update_output_accessibility(self)

    def _load_presets_async(self) -> None:
        presets_path = self._preset_root()
        if not presets_path:
            return
        self.preset_status_label.setText("Loading presets...")
        self.preset_status_label.setAccessibleDescription("Loading presets.")
        self._runner.run(
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
        log.info(f"Loaded {len(self._presets)} preset(s) for GUI selection")

    def _on_presets_failed(self, tb: str) -> None:
        self._presets = []
        self.available_preset_list.clear()
        self.preset_status_label.setText("Preset load failed")
        self.preset_status_label.setAccessibleDescription("Preset loading failed.")
        self._sync_single_preset_notice()
        log.error(f"Preset loading failed:\n{tb}")

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

    def _add_all_presets(self) -> None:
        """Move every loaded preset into the selected list (skipping any already selected)."""
        selected = set(self._selected_preset_paths())
        for preset in self._presets:
            if str(preset.path) not in selected:
                self._add_list_item(self.selected_preset_list, preset)
        if self.selected_preset_list.count():
            self.auto_preset_check.setChecked(False)
        self._render_available_presets()
        self._sync_single_preset_notice()

    def _remove_all_presets(self) -> None:
        self.selected_preset_list.clear()
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
        log.info(f"Loaded profile {path} with {len(preset_paths)} preset(s)")

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
        log.info(f"Saved profile {written} with {len(selected_paths)} preset(s)")

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
        visible = _should_show_single_preset_notice(
            selected_mode, selected_paths, len(self._presets), self.source_edit.text()
        )
        self.single_preset_notice_label.setVisible(visible)
        self.single_preset_notice_label.setAccessibleDescription(
            "One preset will be applied without filename selector filtering." if visible else ""
        )

    def _run_process(self) -> None:
        # Dump mode (the "Dump full UFDR" toggle) runs the standalone UFDR parser instead of
        # the transform pipeline: no presets/entity/exports apply, so it branches out here
        # before any of the transform-path validation below.
        if self.dump_full_ufdr_check.isChecked():
            self._run_ufdr_dump()
            return

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
        # The case name is the run's identity: the case DB is <output>/<name>.matlas.sqlite,
        # so a run targeting an existing case DB APPENDS to it (pipeline.process). Empty falls
        # back to the pipeline default ("merged"), and no explicit human case label is sent.
        case_name = self.case_name_edit.text().strip()
        selected_mode = not self.auto_preset_check.isChecked()
        selected_paths = [Path(path) for path in self._selected_preset_paths()]
        if selected_mode and not selected_paths:
            self._show_error("Selected-presets mode needs at least one preset.")
            return
        single_preset = self._single_preset_for_selector_skip(selected_mode, selected_paths)

        self._set_running(True)
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        # Read the checked exporters on the GUI thread; the worker only uses this captured list.
        exports = self._selected_exports()
        self._apply_log_level()

        # Build the process() keyword arguments once; they are identical across preset modes,
        # which differ only in how the presets path is resolved (see _resolve_presets_path).
        kwargs = dict(
            base_name=case_name or "merged",
            case=case_name or None,
            traceability_format=self.traceability_combo.currentText(),
            entity=entity,
            linked_entity=linked_entity,
            include_source_columns=self.include_source_columns_check.isChecked(),
            exports=exports,
            # The worker reports phases through this thread-safe signal emit (see ProgressBridge).
            progress=self._progress_bridge.phase.emit,
        )

        def task() -> ProcessResult:
            # ExitStack owns any TemporaryDirectory created for the selected-presets folder, so
            # process() runs while it is still alive and it is cleaned up once process() returns.
            with ExitStack() as stack:
                presets_path = self._resolve_presets_path(stack, single_preset, selected_mode, preset_root, selected_paths)
                return process(Path(source), presets_path, output_path, **kwargs)

        self._runner.run(task, self._on_process_done, self._on_process_failed)

    def _resolve_presets_path(
        self,
        stack: ExitStack,
        single_preset: Path | None,
        selected_mode: bool,
        preset_root: Path,
        selected_paths: list[Path],
    ) -> Path:
        """Resolve the presets path passed to process() for the current preset mode.

        Three cases collapse to one process() call: the single preset's path straight through
        (force-preset mode applies it without filename selector filtering for a single-file
        source), the preset root (auto matching), or a temporary folder assembled from the
        selected presets (registered on the ExitStack so it lives for the run and is then removed).
        """
        if single_preset is not None:
            return single_preset
        if not selected_mode:
            return preset_root
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="matlas-presets-"))
        return build_profile_preset_folder(selected_paths, Path(tmp))

    def _on_process_done(self, result: ProcessResult) -> None:
        self._set_running(False)
        # The case database is always written (the run itself), so the output folder always
        # exists and can be opened.
        self._last_output_folder = result.output_case_db.parent
        self.open_output_folder_button.setEnabled(True)
        log.info(f"Case database: {result.output_case_db}")
        # Make this the active case: title the window, bind the analysis tabs to it, and log
        # the case's running totals (which grow as more evidence is appended).
        self._activate_case(result.output_case_db)
        counts = result.row_counts
        if counts.get("rows", 0) == 0:
            self._set_status("Completed: no rows matched a preset; the case database records the run")
            log.warning("No rows matched a preset; the case database records the run.")
            return
        summary = (
            f"Completed: sources={counts.get('sources', 0)} "
            f"matched={counts.get('matched', 0)} rows={counts.get('rows', 0)} "
            f"ranked={counts.get('ranked', 0)}"
        )
        self._set_status(summary)
        log.info(summary)
        if result.output_csv is not None:
            log.info(f"CSV: {result.output_csv}")
        for extra in result.output_extras:
            log.info(f"Export: {extra}")
        if result.output_traceability is not None:
            log.info(f"Traceability: {result.output_traceability}")
        if result.output_warnings is not None:
            log.info(f"Warnings report: {result.output_warnings}")

    def _on_process_failed(self, tb: str) -> None:
        self._set_running(False)
        last = tb.strip().splitlines()[-1] if tb.strip() else "Processing failed"
        self._set_status("Failed")
        log.error(f"Processing failed:\n{tb}")
        self._show_error(last)

    # --- UFDR dump mode (the standalone parser, no transform / no case) --------

    def _run_ufdr_dump(self) -> None:
        """Dump every Cellebrite model in the source ``.ufdr`` to per-model CSVs under the
        output folder. Independent of presets and the case model — it writes no run DB, so it
        needs only a source file and an output folder (the transform fields are ignored)."""
        source = self.source_edit.text().strip()
        output = self.output_edit.text().strip()
        if not source:
            self._show_error("Choose a source .ufdr file.")
            return
        source_path = Path(source).expanduser()
        if not source_path.is_file() or source_path.suffix.lower() != ".ufdr":
            self._show_error("UFDR dump mode needs a .ufdr file as the source.")
            return
        if not output:
            self._show_error("Choose an output folder.")
            return
        output_path = Path(output).expanduser()

        self._set_running(True)
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        self._apply_log_level()

        def task() -> "DumpSummary":
            # Lazy import: the UFDR parser and its streaming readers load only when a dump is
            # actually run, keeping normal GUI start-up light (the codebase's ufdr convention).
            from ufdr_parser.dump import run_dump
            return run_dump(source_path, output_path)

        self._runner.run(
            task,
            lambda summary: self._on_dump_done(summary, output_path),
            self._on_process_failed,
        )

    def _on_dump_done(self, summary: "DumpSummary", out_dir: Path) -> None:
        self._set_running(False)
        self._last_output_folder = out_dir
        self.open_output_folder_button.setEnabled(True)
        status = (
            f"Dumped {summary.record_count} record(s) from {summary.source_count} "
            f"source(s) to {len(summary.files)} CSV file(s)"
        )
        self._set_status(status)
        log.info(status)
        for name, rows in summary.files.items():
            log.info(f"  {name}: {rows} row(s)")

    # --- active case (the top-level context across the three tabs) ------------

    def _case_name_of(self, db_path: Path) -> str:
        """The case's display name: its DB stem without the ``.matlas.sqlite`` suffix."""
        return db_path.name.removesuffix(".matlas.sqlite")

    def _activate_case(self, db_path: Path) -> None:
        """Make ``db_path`` the active case: title the window, bind the Database-Edit and
        Spatiotemporal tabs to it, and show its running totals. Best-effort — a summary read
        failure is logged and swallowed so a completed run is never reported as failed."""
        self._active_case_db = db_path
        self._bind_case_to_panels(db_path)
        self._update_window_title()
        try:
            counts = read_case_counts(db_path)
        except Exception:  # noqa: BLE001 - the summary is feedback, not run correctness.
            log.debug("Could not read case summary", exc_info=True)
            self.case_summary_label.setText(f"Case: {self._case_name_of(db_path)}")
            return
        summary = format_case_summary(self._case_name_of(db_path), counts)
        self.case_summary_label.setText(summary)
        log.info(summary)

    def _clear_active_case(self) -> None:
        """Detach the open case: reset the context, the window title and the summary line."""
        self._active_case_db = None
        self._update_window_title()
        self.case_summary_label.setText("No case open")

    def _new_case(self) -> None:
        """Start a fresh case: clear the case name and detach the open case, keeping the output
        folder and presets so the operator can build a new case in place."""
        self.case_name_edit.clear()
        self._clear_active_case()

    def _open_case(self) -> None:
        """Open an existing case database: bind it as the active case and fill the case name /
        output folder so a subsequent run APPENDS to it (pipeline.process create-or-append)."""
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Open a case database",
            self.output_edit.text() or str(Path.cwd()),
            "mATLAS case database (*.matlas.sqlite);;All files (*)",
        )
        if not path:
            return
        db_path = Path(path)
        if not is_matlas_run_db(db_path):
            self._show_error(f"Not a mATLAS case database: {db_path.name}")
            return
        self.output_edit.setText(str(db_path.parent))
        self.case_name_edit.setText(self._case_name_of(db_path))
        self._activate_case(db_path)
        log.info(f"Opened case {db_path}")

    def _bind_case_to_panels(self, db_path: Path) -> None:
        """Point the Database-Edit and Spatiotemporal tabs at the active case DB, so the two
        analysis tabs operate on the very case the Transform tab just built or appended to."""
        text = str(db_path)
        if self._db_edit_controller is not None:
            self._db_edit_controller.path_edit.setText(text)
        if self._spatiotemporal_controller is not None:
            self._spatiotemporal_controller.db_edit.setText(text)

    def _update_window_title(self) -> None:
        """Show the active case in the window title (or the bare app name when none is open)."""
        if self._active_case_db is None:
            self.window.setWindowTitle(WINDOW_TITLE_BASE)
            return
        self.window.setWindowTitle(f"{WINDOW_TITLE_BASE} — Case: {self._case_name_of(self._active_case_db)}")

    def _set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.run_button.setText("Running..." if running else "Run")
        if running:
            self._current_phase = ""
            self._anim_dots = 0
            self._run_timer.start()
            self._tick_running_animation()
        else:
            self._run_timer.stop()
            self._set_status("Idle")

    def _running_base_text(self) -> str:
        return f"Running - {self._current_phase}" if self._current_phase else "Running"

    @Slot(str)
    def _on_phase(self, name: str) -> None:
        """A new pipeline phase started: announce it (accessible), and reset the dot animation."""
        self._current_phase = name
        self._anim_dots = 0
        self._set_status(self._running_base_text())

    def _tick_running_animation(self) -> None:
        # Cycle '.'/'..'/'...' after the phase name. Set the label text directly (not via
        # _set_status) so the accessible description is not re-announced three times a second.
        self._anim_dots = (self._anim_dots % 3) + 1
        self.status_label.setText(f"{self._running_base_text()} {'.' * self._anim_dots}")

    def _open_output_folder(self) -> None:
        if self._last_output_folder is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_folder)))

    def _open_error_reports(self) -> None:
        """Open the crash-report folder (model_atlas.app.diagnostics writes matlas_error_*.json here
        on any unhandled error). Always available; if nothing has failed yet the folder may not
        exist, so tell the user rather than opening a missing path."""
        crash_dir = Path(get_settings().diagnostics.crash_dir).expanduser()
        if not crash_dir.is_absolute():
            crash_dir = Path.cwd() / crash_dir
        if not crash_dir.is_dir():
            QMessageBox.information(
                self.window, "Model Atlas",
                f"No error reports yet.\nThey would be written to:\n{crash_dir}",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(crash_dir)))

    def _clear_form(self) -> None:
        self.source_edit.clear()
        self.output_edit.clear()
        self.case_name_edit.clear()
        # Clearing the form closes the active case: reset the context, title and summary line.
        self._clear_active_case()
        self.entity_edit.clear()
        self.linked_entity_edit.clear()
        self.presets_path_edit.setText(str(Path.cwd() / "presets"))
        self.preset_search_edit.clear()
        self.selected_preset_list.clear()
        self.traceability_combo.setCurrentText("readable")
        self.log_level_combo.setCurrentText("INFO")
        for action in self._export_actions.values():
            action.setChecked(False)
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
