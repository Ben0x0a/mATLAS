"""Runtime-loaded PySide6 GUI controller for Model Atlas.

Defines:    MainController, the sole controller for the single-window GUI. It builds the
            window from main_window.ui, wires widget signals, dispatches pipeline runs onto a
            background thread, and exposes shutdown() — the ordered teardown of everything it
            owns that outlives the window (run timer, progress bridge, log handler, workers).
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
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QFile, QObject, QStringListModel, QTimer, QUrl, Qt, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFontMetrics, QGuiApplication, QIcon
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import accessibility, header_assets, report_actions
from gui.case_state import format_case_summary, read_case_counts
from gui.completion import param_completions
from gui.db_edit_controller import DbEditController
from gui.spatiotemporal_controller import SpatiotemporalController
from gui.widgets.checkable_menu import CheckableMenu
from gui.widgets.drag_drop_line_edit import DragDropLineEdit
from gui.theme import build_stylesheet, configure_application_font
from gui.worker import ProgressBridge, TaskRunner, install_gui_log_handler, remove_gui_log_handler
from model_atlas.app.profiles import PROFILE_SUFFIX, build_profile_preset_folder, load_profile, save_profile
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

# How much content the two elastic areas must always show, expressed in rows/lines rather
# than pixels so the promise survives a larger system text size. Applied by
# MainController._scale_minimums_to_font. The half row is deliberate: a part-row at the
# bottom edge is what tells the reader the list scrolls.
_PRESET_ROWS_VISIBLE = 3.5
_LOG_LINES_VISIBLE = 4

# A case name becomes a filename (``<name>.matlas.sqlite``), so these would either redirect
# the database out of the chosen folder or produce a name the filesystem rejects. Checked in
# _new_case; Open case cannot hit it because the name is derived from an existing file.
_CASE_NAME_FORBIDDEN = '/\\:*?"<>|'



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
        # The case the GUI is working in, in two parts. The DECLARED identity (name +
        # folder) comes from New case / Open case and is what a run writes to; it replaces
        # the old "Case name" and "Output folder" form fields, which is why there is state
        # here and no longer a widget to read. The PERSISTED database is set once a run has
        # actually written one, and is what the analysis tabs bind to — a freshly declared
        # case has no database on disk yet, so the two cannot be collapsed into one field.
        self._case_name: str | None = None
        self._case_output_dir: Path | None = None
        self._active_case_db: Path | None = None
        self._status_text = "Idle"
        self._log_handler: logging.Handler | None = None
        self._shutdown_done = False
        self._current_phase = ""
        self._anim_dots = 0
        # Output folder of the dump in flight, read by _on_dump_done on the GUI thread.
        self._pending_dump_dir: Path | None = None

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
            app.aboutToQuit.connect(self.shutdown)
        QTimer.singleShot(0, self._load_presets_async)

    def shutdown(self) -> None:
        """Detach everything of this controller's that outlives the window, in the only safe order.

        WHY: the controller owns emitters whose lifetime is NOT the window's — the 350 ms run
        timer, the progress bridge, the root-logger GUI handler, and background workers whose
        results come back as QUEUED callbacks into methods that touch widgets. Any one of them
        firing after the window is destroyed dereferences freed memory and kills the process
        (observed as an intermittent crash inside Qt's timer dispatch at teardown).

        HOW: silence the emitters first (timer, progress bridge), then join the worker threads,
        then deliver what they already posted while the widgets still exist, and only then
        detach the log handler — last, so records emitted during that drain still reach the log
        panel. Idempotent, because it is wired to ``aboutToQuit`` AND called explicitly by the
        GUI tests' fixture teardown.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._run_timer.stop()
        self._progress_bridge.phase.disconnect(self._on_phase)
        # The spatiotemporal panel owns emitters of its own (its progress bridge); silence
        # them here too, before this controller's widgets — which include that panel's — die.
        if self._spatiotemporal_controller is not None:
            self._spatiotemporal_controller.shutdown()
        self._runner.wait_all()
        if (app := QCoreApplication.instance()) is not None:
            app.processEvents()
        self._detach_log_handler()

    def _detach_log_handler(self) -> None:
        """Remove the GUI log handler from the root logger so no post-teardown record touches
        the destroyed log widget."""
        remove_gui_log_handler(self._log_handler)
        self._log_handler = None

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

        # The three path fields are bound by their promoted type, not by QLineEdit. If the
        # .ui promotion or the registerCustomWidget call above is ever lost, QUiLoader
        # quietly substitutes a plain QLineEdit and drag-and-drop simply stops working;
        # asking for the real class here turns that into a startup error instead.
        self.source_edit: DragDropLineEdit = child(DragDropLineEdit, "sourceEdit")
        self.presets_path_edit: DragDropLineEdit = child(DragDropLineEdit, "presetsPathEdit")
        self.entity_edit: QLineEdit = child(QLineEdit, "entityEdit")
        self.concerns_edit: QLineEdit = child(QLineEdit, "concernsEdit")
        self.preset_search_edit: QLineEdit = child(QLineEdit, "presetSearchEdit")
        self.available_preset_list: QListWidget = child(QListWidget, "availablePresetList")
        self.selected_preset_list: QListWidget = child(QListWidget, "selectedPresetList")
        self.log_edit: QTextEdit = child(QTextEdit, "logEdit")
        self.status_label: QLabel = child(QLabel, "statusLabel")
        self.preset_status_label: QLabel = child(QLabel, "presetStatusLabel")
        self.single_preset_notice_label: QLabel = child(QLabel, "singlePresetNoticeLabel")
        self.app_title_label: QLabel = child(QLabel, "appTitleLabel")
        self.case_summary_label: QLabel = child(QLabel, "caseSummaryLabel")
        self.new_case_button: QPushButton = child(QPushButton, "newCaseButton")
        self.open_case_button: QPushButton = child(QPushButton, "openCaseButton")
        self.close_case_button: QPushButton = child(QPushButton, "closeCaseButton")

        self.source_file_button: QPushButton = child(QPushButton, "sourceFileButton")
        self.source_folder_button: QPushButton = child(QPushButton, "sourceFolderButton")
        self.presets_folder_button: QPushButton = child(QPushButton, "presetsFolderButton")
        self.presets_file_button: QPushButton = child(QPushButton, "presetsFileButton")
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
        default_presets = Path.cwd() / "presets" / "mATLAS-presets"
        self.presets_path_edit.setText(str(default_presets))
        self.log_edit.setAcceptRichText(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self._size_window_to_screen(1060, 940)
        self.window.setWindowTitle(WINDOW_TITLE_BASE)
        self.window.setWindowIcon(QIcon(str(header_assets.asset_path(header_assets.DESKTOP_ICON_ASSET))))
        self.window.setStyleSheet(build_stylesheet())
        header_assets.configure_header_assets(self.window)
        self._configure_layout_stretch()
        self._configure_preset_selection()
        self._configure_ufdr_toggle()
        self.single_preset_notice_label.setVisible(False)
        accessibility.configure_status_label(self)
        self._scale_minimums_to_font()
        self._stabilize_form_labels()
        self._stabilize_preset_mode_row()
        self._sync_case_bar()
        self._configure_entity_completion()
        self._configure_exports()
        report_actions.sync_report_button(self.open_error_report_button)
        accessibility.configure_accessibility(self)

    def _attach_panels(self) -> None:
        """Build the two MVC panels inside their tab placeholders.

        Each controller owns its whole panel and takes only the tab QWidget; all logic
        lives in the model layer (edit_api / analyse.concomitance) — the shell just
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
        # Share the app's single lifecycle-managed runner with the spatiotemporal panel too, so
        # its concomitance query is waited on aboutToQuit (see SpatiotemporalController).
        self._spatiotemporal_controller = SpatiotemporalController(
            self.spatiotemporal_tab, runner=self._runner)

    def _configure_exports(self) -> None:
        """Populate the 'Additional export' button with a checkable menu, one item per registered
        exporter (built-ins + any drop-in plugins). The selection is read at run time and
        passed to process().

        The menu is a CheckableMenu so it stays open across ticks: these formats are
        independent choices, and a plain QMenu closed after each one, forcing the user to
        reopen it once per format and hiding the current selection while they chose."""
        load_builtin_exporters()
        menu = CheckableMenu(self.additional_export_button)
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
        """Give the entity / concerns fields a completer: the call-form keywords, plus
        ``field(<column>)`` per column when the input is a single CSV (see gui.completion)."""
        self._entity_completion_model = QStringListModel(self.window)
        for edit in (self.entity_edit, self.concerns_edit):
            completer = QCompleter(self.window)
            completer.setModel(self._entity_completion_model)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            edit.setCompleter(completer)
        self._refresh_entity_completion()

    def _refresh_entity_completion(self) -> None:
        """Rebuild the entity/concerns completion list for the current input path."""
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
        self.close_case_button.clicked.connect(self._close_case)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.open_error_report_button.clicked.connect(self._on_report_button)
        self.clear_form_button.clicked.connect(self._clear_form)
        self.clear_log_button.clicked.connect(self.log_edit.clear)
        self.load_profile_button.clicked.connect(self._load_profile)
        self.save_profile_button.clicked.connect(self._save_profile)
        self.reload_presets_button.clicked.connect(self._load_presets_async)
        self.auto_preset_check.toggled.connect(lambda _checked: self._sync_single_preset_notice())
        self.log_level_combo.currentTextChanged.connect(self._apply_log_level)
        self.dump_full_ufdr_check.toggled.connect(lambda _checked: self._sync_ufdr_mode())

    def _size_window_to_screen(self, width: int, height: int) -> None:
        """Open at the preferred size, but never larger than the screen it opens on.

        A constant like 1060x940 is comfortable on a desktop and taller than the work area
        of a 1366x768 laptop, where it would push the action row and the log off the bottom
        edge with no way to drag them back. Clamping to the available geometry (which
        already excludes the taskbar / dock / menu bar) keeps the whole form reachable on
        every screen without special-casing any platform."""
        screen = self.window.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, available.width() - 40)
            height = min(height, available.height() - 40)
        self.window.resize(width, height)

    def _configure_preset_selection(self) -> None:
        """Let both preset lists take a multiple selection.

        Shift+click extends a range and Ctrl/Cmd+click picks individual entries, which is
        the platform-standard behaviour users already expect from a list this shape. The
        transfer handlers were written against selectedItems() from the start, so they
        already move whatever is highlighted — only the selection mode was holding it to
        one preset at a time."""
        for list_widget in (self.available_preset_list, self.selected_preset_list):
            list_widget.setSelectionMode(
                QAbstractItemView.SelectionMode.ExtendedSelection
            )

    def _configure_layout_stretch(self) -> None:
        """Decide which rows and columns absorb the window's spare space.

        This lives here rather than in main_window.ui on purpose: QUiLoader silently drops
        a layout's ``stretch`` / ``columnStretch`` properties (they are honoured only by
        uic at code-generation time), so setting them in the .ui looks correct and does
        nothing. Without them Qt spread the surplus evenly over every row; combined with
        the stacked minimum heights that pushed the total past the window and left the
        action row painted on top of the preset card.
        """
        if (main_layout := self.window.centralWidget().layout()) is not None:
            main_layout.setStretch(0, 0)        # header filmstrip: natural height
            main_layout.setStretch(1, 1)        # tab shell takes the rest

        # Transform page: the case bar, the input card and the action row keep their natural
        # height, and the surplus is split between the preset picker and the log — two parts
        # to the log against one to the picker. The picker is a chooser, not a workspace:
        # past a handful of visible rows the extra height buys nothing, whereas the log is
        # what the user actually reads during a run.
        if (form := self.window.findChild(QVBoxLayout, "transformTabLayout")) is not None:
            for index, stretch in enumerate((0, 0, 1, 0, 2)):
                form.setStretch(index, stretch)

        # Input card: only the field column absorbs width, so the browse buttons stay a
        # stable target and the captions never drift away from their fields.
        if (grid := self.window.findChild(QGridLayout, "inputGrid")) is not None:
            for column, stretch in enumerate((0, 1, 0, 0)):
                grid.setColumnStretch(column, stretch)

        # The two preset lists share the width evenly; the transfer buttons between them
        # keep their natural size rather than stretching into oversized targets.
        if (lists_row := self.window.findChild(QHBoxLayout, "presetListsRow")) is not None:
            for index, stretch in enumerate((1, 0, 1)):
                lists_row.setStretch(index, stretch)

    def _scale_minimums_to_font(self) -> None:
        """Re-express the hand-tuned minimum sizes in terms of the current font.

        Those constants live in main_window.ui as pixels, which quietly means something
        different for every reader: the preset lists were sized to show about three and a
        half rows, but a user running a larger system text size for readability got three,
        then two — the accessibility setting bought bigger glyphs and took away content.
        Deriving each minimum from the font metrics keeps the intent ("three and a half
        rows", "four log lines", "the placeholder fits") true at any text size.

        Qt's high-DPI scaling is a separate axis and is already handled: it scales the whole
        UI including these values. This covers the other axis — text size at a fixed DPI."""
        metrics = QFontMetrics(self.window.font())
        line_height = metrics.height()
        # One list row is the text plus the QSS item padding (3px top and bottom) and its
        # 1px transparent border; the list itself adds 3px of padding and a 1px frame.
        row_height = line_height + 8
        list_minimum = int(row_height * _PRESET_ROWS_VISIBLE) + 8
        for list_widget in (self.available_preset_list, self.selected_preset_list):
            list_widget.setMinimumHeight(list_minimum)
        self.log_edit.setMinimumHeight(line_height * _LOG_LINES_VISIBLE + 10)
        # Wide enough that the search placeholder is never elided into meaninglessness.
        self.preset_search_edit.setMinimumWidth(
            metrics.horizontalAdvance(self.preset_search_edit.placeholderText()) + 30
        )

    def _stabilize_form_labels(self) -> None:
        """Align the input grid's row captions on one column edge.

        The width is measured from the widest caption rather than pinned to a constant, so
        the column still fits when the text is translated or when the user runs a larger
        system font — a hardcoded value clipped in both cases."""
        labels = [
            label
            for name in (
                "sourceLabel",
                "identityLabel",
                "presetsPathLabel",
                "outputModeLabel",
                "optionsLabel",
            )
            if (label := self.window.findChild(QLabel, name)) is not None
        ]
        if not labels:
            return
        column_width = max(label.sizeHint().width() for label in labels) + 8
        for label in labels:
            label.setMinimumWidth(column_width)

    def _configure_ufdr_toggle(self) -> None:
        """Show the UFDR-parser toggle and reflect its state in the header title.

        When checked, Run invokes the standalone UFDR parser (dump every Cellebrite model to
        CSV) instead of the transform pipeline — see ``_run_ufdr_dump``.
        """
        self.dump_full_ufdr_check.setVisible(True)
        self.dump_full_ufdr_check.setEnabled(True)
        self._sync_ufdr_mode()

    def _sync_ufdr_mode(self) -> None:
        """Reflect the UFDR-dump toggle: swap the header title AND grey out the controls that
        only apply to the transform pipeline. In dump mode only the source and output matter,
        so disabling the preset/entity/export controls signals they are ignored."""
        dump = self.dump_full_ufdr_check.isChecked()
        self.app_title_label.setText(APP_TITLE_UFDR if dump else APP_TITLE_DEFAULT)
        # The header filmstrip depicts the transform pipeline, which dump mode does not run.
        header_assets.set_transition_visible(self.window, not dump)
        self._set_transform_controls_enabled(not dump)

    def _set_transform_controls_enabled(self, enabled: bool) -> None:
        """Enable/disable every control that only feeds the transform pipeline — everything
        except the source, the output folder and Run (which both modes need)."""
        for widget in (
            self.presets_path_edit, self.preset_search_edit,
            self.available_preset_list, self.selected_preset_list,
            self.presets_folder_button, self.presets_file_button,
            self.add_preset_button, self.remove_preset_button,
            self.add_all_presets_button, self.remove_all_presets_button,
            self.reload_presets_button, self.auto_preset_check,
            self.entity_edit, self.concerns_edit,
            self.include_source_columns_check, self.traceability_combo,
            self.additional_export_button,
        ):
            widget.setEnabled(enabled)

    def eventFilter(self, watched: QObject, event: object) -> bool:
        accessibility.handle_status_resize(self, watched, event)
        return super().eventFilter(watched, event)

    def _set_status(self, text: str) -> None:
        accessibility.set_status(self, text)

    def _stabilize_preset_mode_row(self) -> None:
        """Keep the 'Auto apply' box at its natural width so the status text beside it does
        not shift as the row's other contents change. sizeHint (not a constant) is used so
        the caption still fits under a larger system font."""
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
        # The filter must list every format the ingest layer actually reads, or a supported
        # file is simply invisible here. It previously omitted *.xml (a registered reader —
        # the shipped Cellebrite XML presets target it) and *.ufdr (which dump mode outright
        # requires), so browsing to either was impossible without switching to "All files".
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Choose source file",
            self.source_edit.text() or str(Path.cwd()),
            "Supported sources (*.csv *.tsv *.xlsx *.xlsm *.xltx *.xltm *.xml"
            " *.db *.sqlite *.sqlite3 *.zip *.ufdr);;All files (*)",
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
            self.presets_path_edit.text() or str(Path.cwd() / "presets" / "mATLAS-presets"),
        )
        if path:
            self.presets_path_edit.setText(path)
            self._load_presets_async()

    def _browse_presets_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Choose preset YAML",
            self.presets_path_edit.text() or str(Path.cwd() / "presets" / "mATLAS-presets"),
            "YAML presets (*.yaml *.yml);;All files (*)",
        )
        if path:
            self.presets_path_edit.setText(path)
            self._load_presets_async()

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
        self._select_presets(self.available_preset_list.selectedItems())

    def _remove_selected_presets(self) -> None:
        for item in self.selected_preset_list.selectedItems():
            row = self.selected_preset_list.row(item)
            self.selected_preset_list.takeItem(row)
        self._render_available_presets()
        self._sync_single_preset_notice()

    def _add_all_presets(self) -> None:
        """Move every preset the available list currently SHOWS into the selected list.

        WHY the list widget rather than ``self._presets``: with a search term typed, the
        available list shows a subset, and "add all" next to a search box means "all of
        these" — adding the hidden ones too would silently contradict what the analyst
        can see. ``_render_available_presets`` is the single place the visible set is
        decided, so reading its output keeps the two in step by construction.
        """
        self._select_presets(
            self.available_preset_list.item(row)
            for row in range(self.available_preset_list.count())
        )

    def _select_presets(self, items: Iterable[QListWidgetItem]) -> None:
        """Move ``items`` from the available list to the selected one, skipping duplicates."""
        paths = set(self._selected_preset_paths())
        by_path = {str(preset.path): preset for preset in self._presets}
        for item in items:
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

    def _reject_if_blank(self, text: str, message: str) -> bool:
        """Show ``message`` and return True when ``text`` is blank; a no-op returning False
        otherwise. Both run modes validate a required source/output field this way."""
        if not text:
            self._show_error(message)
            return True
        return False

    def _start_run(self) -> None:
        """The run-preamble shared by both modes: enter the running state, forget the last
        output folder, reset the open-output button and apply the current log level.

        Extracted from ``_run_process`` / ``_run_ufdr_dump`` (which repeated it verbatim);
        each keeps its own MODE-specific validation before calling this. Called only AFTER all
        validation passes, so a rejected run never leaves the UI stuck in the running state."""
        self._set_running(True)
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        self._apply_log_level()

    def _run_process(self) -> None:
        # Dump mode (the "Dump full UFDR" toggle) runs the standalone UFDR parser instead of
        # the transform pipeline: no presets/entity/exports apply, so it branches out here
        # before any of the transform-path validation below.
        if self.dump_full_ufdr_check.isChecked():
            self._run_ufdr_dump()
            return

        source = self.source_edit.text().strip()
        preset_root = self._preset_root()
        if self._reject_if_blank(source, "Choose a source file or folder."):
            return
        if preset_root is None:
            self._show_error("Choose a preset folder or YAML file.")
            return
        if self._case_output_dir is None or self._case_name is None:
            self._show_error("Open or create a case first (New case / Open case).")
            return
        entity = self.entity_edit.text().strip() or None
        concerns = self.concerns_edit.text().strip()
        if not concerns:
            # Concerns is mandatory (matches the CLI's required --concerns):
            # every output row must be attributable to a case subject.
            self._show_error("Enter the concerns (required).")
            return
        # The case bar is the run's identity: the case DB is <folder>/<name>.matlas.sqlite,
        # so a run targeting an existing case DB APPENDS to it (pipeline.process).
        output_path = self._case_output_dir.expanduser()
        case_name = self._case_name
        selected_mode = not self.auto_preset_check.isChecked()
        selected_paths = [Path(path) for path in self._selected_preset_paths()]
        if selected_mode and not selected_paths:
            self._show_error("Selected-presets mode needs at least one preset.")
            return
        single_preset = self._single_preset_for_selector_skip(selected_mode, selected_paths)

        self._start_run()
        # Read the checked exporters on the GUI thread; the worker only uses this captured list.
        exports = self._selected_exports()

        # Build the process() keyword arguments once; they are identical across preset modes,
        # which differ only in how the presets path is resolved (see _resolve_presets_path).
        kwargs = dict(
            base_name=case_name or "merged",
            case=case_name or None,
            traceability_format=self.traceability_combo.currentText(),
            entity=entity,
            concerns=concerns,
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
        # A run may have produced a report; the caption must not lag the folder.
        report_actions.sync_report_button(self.open_error_report_button)
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
        report_actions.sync_report_button(self.open_error_report_button)
        last = tb.strip().splitlines()[-1] if tb.strip() else "Processing failed"
        self._set_status("Failed")
        log.error(f"Processing failed:\n{tb}")
        self._show_error(last)

    # --- UFDR dump mode (the standalone parser, no transform / no case) --------

    def _run_ufdr_dump(self) -> None:
        """Dump every Cellebrite model in the source ``.ufdr`` or report ``.xml`` to per-model
        CSVs under the output folder. Independent of presets and the case model — it writes no
        run DB, so it needs only a source file and an output folder (the transform fields are
        ignored)."""
        # Lazy import, like run_dump below: keeps normal GUI start-up free of the parser.
        from ufdr_parser.archive import input_kind

        source = self.source_edit.text().strip()
        if self._reject_if_blank(source, "Choose a source .ufdr or report .xml file."):
            return
        source_path = Path(source).expanduser()
        # Classified by header, not extension: a PA "XML report" export and a .ufdr are both
        # accepted whatever they are named.
        if not source_path.is_file() or input_kind(source_path) is None:
            self._show_error(
                "UFDR dump mode needs a .ufdr file or a Cellebrite report .xml as the source."
            )
            return
        # A dump writes no case database, but it still needs a destination, and the case
        # folder is the only one the user has now named.
        if self._case_output_dir is None:
            self._show_error("Open or create a case first, to set the output folder.")
            return
        output_path = self._case_output_dir.expanduser()

        self._start_run()

        def task() -> "DumpSummary":
            # Lazy import: the UFDR parser and its streaming readers load only when a dump is
            # actually run, keeping normal GUI start-up light (the codebase's ufdr convention).
            from ufdr_parser.dump import run_dump
            return run_dump(source_path, output_path)

        # A bound method of this QObject, NOT a lambda: PySide6 runs a plain callable connected
        # to the worker's signal on the WORKER thread, so a lambda here touched the widgets and
        # stopped the status timer off the GUI thread ("Timers cannot be stopped from another
        # thread") — undefined behaviour in Qt. A bound slot is queued onto the GUI thread.
        self._pending_dump_dir = output_path
        self._runner.run(task, self._on_dump_done, self._on_process_failed)

    @Slot(object)
    def _on_dump_done(self, summary: "DumpSummary") -> None:
        out_dir = self._pending_dump_dir
        self._set_running(False)
        self._last_output_folder = out_dir
        self.open_output_folder_button.setEnabled(True)
        status = (
            f"Dumped {summary.record_count} record(s) from {summary.source_count} "
            f"source(s) to {len(summary.files)} CSV file(s)"
        )
        failed = [brick for brick in summary.bricks if brick.error]
        if failed:
            status += f"; {len(failed)} post-dump step(s) failed (see log)"
        self._set_status(status)
        log.info(status)
        for name, rows in summary.files.items():
            log.info(f"  {name}: {rows} row(s)")
        for brick in summary.bricks:
            if brick.error:
                log.error(f"  {brick.name} failed: {brick.error}")
            for path in brick.files:
                log.info(f"  {brick.name}: {path.name}")

    # --- active case (the top-level context across the three tabs) ------------

    def _case_name_of(self, db_path: Path) -> str:
        """The case's display name: its DB stem without the ``.matlas.sqlite`` suffix."""
        return db_path.name.removesuffix(".matlas.sqlite")

    def _activate_case(self, db_path: Path) -> None:
        """Make ``db_path`` the active case: title the window, bind the Database-Edit tab
        to it, and show its running totals. Best-effort — a summary read failure is logged
        and swallowed so a completed run is never reported as failed."""
        self._active_case_db = db_path
        # A run may create the case a New case declaration only named; keep the two in step.
        if self._case_name is None:
            self._declare_case(self._case_name_of(db_path), db_path.parent)
        self._bind_case_to_panels(db_path)
        self._update_window_title()
        try:
            counts = read_case_counts(db_path)
        except Exception:  # noqa: BLE001 - the summary is feedback, not run correctness.
            log.debug("Could not read case summary", exc_info=True)
            self.case_summary_label.setText(f"Case: {self._case_name_of(db_path)}")
            self._sync_case_bar_buttons()
            return
        summary = format_case_summary(self._case_name_of(db_path), counts)
        self.case_summary_label.setText(summary)
        log.info(summary)
        self._sync_case_bar_buttons()

    def _clear_active_case(self) -> None:
        """Detach the persisted case: reset the context, window title and summary line."""
        self._active_case_db = None
        self._update_window_title()
        self._sync_case_bar()

    def _new_case(self) -> None:
        """Declare a new case: ask for its name, then the folder to write it in.

        The pair replaces the old "Case name" and "Output folder" fields — the case database
        is ``<folder>/<name>.matlas.sqlite`` exactly as before, so this only moves where the
        two values are entered. Either dialog can be cancelled, and nothing changes until
        both have been answered: a half-declared case would be worse than none.

        Pointing a new case at a folder that already holds one of the same name adopts it
        rather than refusing, because a run against an existing case database appends to it
        (pipeline.process create-or-append). Adopting it up front means the summary and the
        analysis tabs show the case's real contents instead of pretending it is empty."""
        name, accepted = QInputDialog.getText(
            self.window, "New case", "Case name:", text=self._case_name or "",
        )
        if not accepted:
            return
        name = name.strip()
        if not name:
            self._show_error("Enter a case name.")
            return
        if invalid := set(name) & set(_CASE_NAME_FORBIDDEN):
            # The name becomes a filename, so a separator would silently redirect the case
            # database out of the chosen folder.
            self._show_error(
                f"A case name cannot contain {' '.join(sorted(invalid))}"
            )
            return
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose the case folder",
            str(self._case_output_dir or Path.cwd()),
        )
        if not folder:
            return
        self._declare_case(name, Path(folder))
        db_path = self._case_db_path()
        if db_path is not None and db_path.is_file() and is_matlas_run_db(db_path):
            self._activate_case(db_path)
            log.info(f"Case {name!r} already exists in {folder}; a run will append to it")
        else:
            self._sync_case_bar()
            log.info(f"New case {name!r} in {folder}")

    def _open_case(self) -> None:
        """Open an existing case database, taking the case name and folder from its path.

        A subsequent run APPENDS to it (pipeline.process create-or-append)."""
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Open a case database",
            str(self._case_output_dir or Path.cwd()),
            "mATLAS case database (*.matlas.sqlite);;All files (*)",
        )
        if not path:
            return
        db_path = Path(path)
        if not is_matlas_run_db(db_path):
            self._show_error(f"Not a mATLAS case database: {db_path.name}")
            return
        self._declare_case(self._case_name_of(db_path), db_path.parent)
        self._activate_case(db_path)
        log.info(f"Opened case {db_path}")

    def _close_case(self) -> None:
        """Close the open case. The case database on disk is left untouched."""
        name = self._case_name
        self._case_name = None
        self._case_output_dir = None
        self._clear_active_case()
        if name:
            log.info(f"Closed case {name!r}")

    def _declare_case(self, name: str, output_dir: Path) -> None:
        """Record the case identity a run will write to, without touching the disk."""
        self._case_name = name
        self._case_output_dir = output_dir

    def _case_db_path(self) -> Path | None:
        """Where this case's database lives (or will live); None when no case is open."""
        if self._case_name is None or self._case_output_dir is None:
            return None
        return self._case_output_dir / f"{self._case_name}.matlas.sqlite"

    def _sync_case_bar_buttons(self) -> None:
        """Show either the two open-a-case buttons or the single Close case button.

        Only one of these states is ever actionable, so showing both at once would offer a
        choice that does not apply. Kept separate from the summary text because
        _activate_case writes its own richer summary and must not have it overwritten."""
        has_case = self._case_name is not None
        self.new_case_button.setVisible(not has_case)
        self.open_case_button.setVisible(not has_case)
        self.close_case_button.setVisible(has_case)
        accessibility.update_case_bar_accessibility(self)

    def _sync_case_bar(self) -> None:
        """Refresh the buttons AND the summary line for a case with no database yet."""
        self._sync_case_bar_buttons()
        if self._case_name is None:
            self.case_summary_label.setText("No case open")
        elif self._active_case_db is None:
            # Declared but not yet written: say so plainly rather than showing zero counts,
            # which would read as "this case ran and matched nothing".
            self.case_summary_label.setText(
                f"Case {self._case_name!r} (new) \u2192 {self._case_output_dir}"
            )

    def _bind_case_to_panels(self, db_path: Path) -> None:
        """Point the Database-Edit tab at the active case DB, so it operates on the very
        case the Transform tab just built or appended to.

        The Spatiotemporal panel is NOT bound here: it owns its own Case 1 / Case 2
        selection (T101a), so inheriting Transform's case would only ever mislabel which
        case is being analysed."""
        if self._db_edit_controller is not None:
            self._db_edit_controller.path_edit.setText(str(db_path))

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

    def _on_report_button(self) -> None:
        """Open the error-report folder, or file a bug report — see gui.report_actions.

        The context tells a reader what the tool was doing when the report was filed, which
        is most of the value when nothing raised."""
        report_actions.handle_report_button(
            self.window, self.open_error_report_button,
            context={"entrypoint": "gui", "tab": self.tab_widget.tabText(
                self.tab_widget.currentIndex()), "case": self._case_name},
        )

    def _clear_form(self) -> None:
        self.source_edit.clear()
        # Clearing the form closes the case: reset the identity, context, title and summary.
        self._close_case()
        self.entity_edit.clear()
        self.concerns_edit.clear()
        self.presets_path_edit.setText(str(Path.cwd() / "presets" / "mATLAS-presets"))
        self.preset_search_edit.clear()
        self.selected_preset_list.clear()
        self.traceability_combo.setCurrentText("readable")
        self.log_level_combo.setCurrentText("INFO")
        for action in self._export_actions.values():
            action.setChecked(False)
        self.auto_preset_check.setChecked(True)
        self._sync_single_preset_notice()
        self._set_status("Idle")
        self._last_output_folder = None
        self.open_output_folder_button.setEnabled(False)
        self._render_available_presets()
        self._load_presets_async()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self.window, "Model Atlas", message)
