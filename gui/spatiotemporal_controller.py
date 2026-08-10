"""The Spatiotemporal Analysis panel controller: which placements of group 1 are near
which placements of group 2.

Defines:    ``SpatiotemporalController`` — builds the concomitance panel INSIDE a parent
            QWidget (the "Spatiotemporal Analysis" tab). Two independently-chosen entity
            groups (each a criteria list + any/all mode), an optional second case, a
            distance threshold and a directional (before/after) time window are built
            into a ``ConcomitanceRequest`` (the Qt-free seam) and handed to
            ``model_atlas.app.concomitance_run.run_concomitance`` OFF-THREAD via the
            shared ``TaskRunner`` — the panel calls only the app layer and the Qt-free
            seam, never the engine or sqlite3 directly. Results go to a CSV file (the run
            can produce far more rows than either input); the panel's job is to
            configure and launch, not to browse.
Used by:    the GUI orchestrator, which constructs one ``SpatiotemporalController`` with
            the Spatiotemporal Analysis tab widget as its parent; tests/gui/
            test_spatiotemporal_panel.py.
Depends on: gui.spatiotemporal_query (the Qt-free UI<->request mapping seam), gui.worker
            (TaskRunner / ProgressBridge — reused, never reinvented), model_atlas.app.
            concomitance_run (run_concomitance / ConcomitanceRunResult),
            model_atlas.analyze.concomitance_request
            (ConcomitanceRequest / Criterion / CriterionKind / EntityGroup / MatchMode /
            Problem / Severity / check_request), model_atlas.analyze.case_vocabulary
            (read_case_vocabulary / VocabularyEntry), model_atlas.ingest.writer
            (is_matlas_run_db), gui.report_actions (the shared diagnostics button),
            PySide6, and the standard library (datetime, threading).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeyEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import report_actions
from gui.spatiotemporal_query import DURATION_UNIT_NAMES, build_request, split_identifiers
from gui.worker import ProgressBridge, TaskRunner
from model_atlas.analyze.case_vocabulary import VocabularyEntry, read_case_vocabulary
from model_atlas.analyze.concomitance_request import (
    Criterion,
    CriterionKind,
    MatchMode,
    Severity,
    check_request,
)
from model_atlas.app.concomitance_run import ConcomitanceRunResult, run_concomitance
from model_atlas.ingest.writer import is_matlas_run_db

log = logging.getLogger("model_atlas.gui")

# The kind combo's display order, each paired with the CriterionKind it selects on.
_KIND_ITEMS: tuple[tuple[str, CriterionKind], ...] = (
    ("Entity type", CriterionKind.ENTITY_TYPE),
    ("Identifier type", CriterionKind.IDENTIFIER_TYPE),
    ("Identifier", CriterionKind.IDENTIFIER),
)

# The two group sides, used throughout as a dict key (never a bare "1"/"2" string, so a
# typo is a KeyError at the point of the typo rather than a silently-ignored no-op).
_SIDE_1 = "group_1"
_SIDE_2 = "group_2"

# The progress bar is a status indicator beside the Run button, not a page element: a
# full-width bar read as the panel's main content and dominated the action row.
_PROGRESS_BAR_WIDTH = 150


# --- one group's widgets + live state (kept together so the two groups build/refresh
# identically, avoiding a hand-duplicated widget set per side) -------------------------

class _GroupWidgets:
    """One group's widgets (kind/value combos, chips list, any/all buttons) + its live
    criteria list and current case-vocabulary snapshot."""

    def __init__(self, root: QWidget, side: str, title: str) -> None:
        self.side = side
        self.criteria: list[Criterion] = []
        self.mode = MatchMode.ANY
        self.vocabulary: dict[CriterionKind, tuple[VocabularyEntry, ...]] = {}

        self.box = QGroupBox(title, root)
        outer = QVBoxLayout(self.box)

        picker_row = QHBoxLayout()
        self.kind_combo = QComboBox(self.box)
        for label, _kind in _KIND_ITEMS:
            self.kind_combo.addItem(label)
        self.value_combo = QComboBox(self.box)
        self.value_combo.setEditable(True)
        self.value_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = QCompleter(self.value_combo.model(), self.value_combo)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.value_combo.setCompleter(completer)
        self.add_button = QPushButton("Add ↓", self.box)
        picker_row.addWidget(self.kind_combo)
        picker_row.addWidget(self.value_combo, 1)
        picker_row.addWidget(self.add_button)
        outer.addLayout(picker_row)

        self.chips_list = QListWidget(self.box)
        self.chips_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        outer.addWidget(self.chips_list, 1)

        # Radio buttons, not a pair of checkable QPushButtons: "exactly one of these two"
        # is what a radio group IS, and Qt's built-in already carries the keyboard and
        # screen-reader behaviour a hand-rolled equivalent would have to re-earn. It also
        # avoids a semantic clash — under this theme a checked QPushButton is filled with
        # the accent colour, which everywhere else in the app means "primary action"
        # (the Run button), not "this option is selected".
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Match", self.box))
        self.any_button = QRadioButton("any of these", self.box)
        self.all_button = QRadioButton("all of these", self.box)
        self.any_button.setChecked(True)   # the default mode (MatchMode.ANY)
        self.mode_group = QButtonGroup(self.box)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.any_button)
        self.mode_group.addButton(self.all_button)
        mode_row.addWidget(self.any_button)
        mode_row.addWidget(self.all_button)
        mode_row.addStretch(1)
        outer.addLayout(mode_row)

    def kind_for_row(self, row: int) -> CriterionKind:
        return _KIND_ITEMS[row][1]

    def current_kind(self) -> CriterionKind:
        return self.kind_for_row(self.kind_combo.currentIndex())


class SpatiotemporalController(QObject):
    """Build and drive the concomitance panel inside a parent tab widget.

    Construct with the Spatiotemporal Analysis tab as ``parent_tab``; the controller lays
    its panel out inside that widget (reusing an existing layout or creating a
    QVBoxLayout on it). The orchestrator wires this in AFTER building the window — this
    file never edits gui.app / main_window.ui / accessibility.py.
    """

    def __init__(self, parent_tab: QWidget, *, runner: TaskRunner | None = None) -> None:
        super().__init__(parent_tab)
        self._parent_tab = parent_tab
        # Reuse the caller's shared runner when given (the app's single lifecycle-managed
        # runner, waited on aboutToQuit) so a query cannot outlive the window; fall back to
        # an own runner parented here for standalone/headless construction.
        self._owns_runner = runner is None
        self._runner = runner or TaskRunner(self)
        self._progress = ProgressBridge(self)
        self._progress.progress.connect(self._on_progress)
        self._shutdown_done = False

        self._case_1_vocabulary: dict[CriterionKind, tuple[VocabularyEntry, ...]] = {}
        self._case_2_vocabulary: dict[CriterionKind, tuple[VocabularyEntry, ...]] = {}
        self._running = False
        self._cancel_event: threading.Event | None = None
        self._last_output_folder: Path | None = None
        self._last_result: ConcomitanceRunResult | None = None

        self._build_ui()
        self._configure_accessibility()
        self._wire_signals()
        self._refresh_group(_SIDE_1)
        self._refresh_group(_SIDE_2)

    def shutdown(self) -> None:
        """Silence this panel's emitters and join any worker it owns, before its widgets die.

        WHY: like MainController, this controller owns emitters whose lifetime is NOT the
        widget's — the progress bridge, and (when constructed standalone) its own TaskRunner.
        A queued progress signal or a finishing worker arriving after the panel's widgets are
        destroyed dereferences freed memory and kills the process, seen as an intermittent
        segfault inside Qt's timer dispatch at teardown.

        HOW: disconnect the bridge first so nothing new is posted, then join the workers ONLY
        if this panel created them — a shared runner belongs to MainController, which waits it
        in its own shutdown, and joining it twice would be wrong here. Idempotent, so a caller
        may pair it with an app-level shutdown without ordering rules.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._progress.progress.disconnect(self._on_progress)
        if self._owns_runner:
            self._runner.wait_all()
            if (app := QCoreApplication.instance()) is not None:
                app.processEvents()

    # --- construction -----------------------------------------------------------------

    def _build_ui(self) -> None:
        self.root = QWidget(self._parent_tab)
        outer = QVBoxLayout(self.root)

        outer.addWidget(self._build_cases_box())

        groups_row = QHBoxLayout()
        self._groups: dict[str, _GroupWidgets] = {
            _SIDE_1: _GroupWidgets(self.root, _SIDE_1, "Group 1 — Case 1"),
            _SIDE_2: _GroupWidgets(self.root, _SIDE_2, "Group 2 — Case 1"),
        }
        groups_row.addWidget(self._groups[_SIDE_1].box, 1)
        groups_row.addWidget(self._groups[_SIDE_2].box, 1)
        outer.addLayout(groups_row)

        outer.addWidget(self._build_thresholds_box())
        outer.addWidget(self._build_results_box())
        outer.addLayout(self._build_action_bar())

        self.log_edit = QTextEdit(self.root)
        self.log_edit.setObjectName("spatiotemporalLogEdit")
        self.log_edit.setReadOnly(True)
        outer.addWidget(self.log_edit, 1)

        layout = self._parent_tab.layout()
        if layout is None:
            layout = QVBoxLayout(self._parent_tab)
        layout.addWidget(self.root)

        self._refresh_chips(_SIDE_1)
        self._refresh_chips(_SIDE_2)

    def _build_cases_box(self) -> QGroupBox:
        box = QGroupBox("Cases", self.root)
        outer = QVBoxLayout(box)

        row_1 = QHBoxLayout()
        self.case_1_edit = QLineEdit(box)
        self.case_1_edit.setObjectName("case1Edit")
        self.case_1_edit.setReadOnly(True)
        self.case_1_edit.setPlaceholderText("path to a <name>.matlas.sqlite case")
        self.case_1_button = QPushButton("Open case…", box)
        row_1.addWidget(QLabel("Case 1", box))
        row_1.addWidget(self.case_1_edit, 1)
        row_1.addWidget(self.case_1_button)
        outer.addLayout(row_1)

        self.compare_check = QCheckBox("Compare against a second case", box)
        outer.addWidget(self.compare_check)

        row_2 = QHBoxLayout()
        self.case_2_edit = QLineEdit(box)
        self.case_2_edit.setObjectName("case2Edit")
        self.case_2_edit.setReadOnly(True)
        self.case_2_edit.setPlaceholderText("path to a second <name>.matlas.sqlite case")
        self.case_2_edit.setEnabled(False)
        self.case_2_button = QPushButton("Open case…", box)
        self.case_2_button.setEnabled(False)
        row_2.addWidget(QLabel("Case 2", box))
        row_2.addWidget(self.case_2_edit, 1)
        row_2.addWidget(self.case_2_button)
        outer.addLayout(row_2)

        return box

    def _build_thresholds_box(self) -> QGroupBox:
        """The two threshold rows, laid out on ONE grid.

        A grid rather than two independent QHBoxLayouts: the rows carry different
        controls (one spin box vs two spin/unit pairs), so side-by-side box layouts gave
        every row its own column positions and nothing lined up vertically. Sharing a
        grid means the captions, the value fields and the unit combos each sit on one
        column edge, which is what makes the block readable at a glance.
        """
        box = QGroupBox("Thresholds", self.root)
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("Distance", box), 0, 0)
        self.space_spin = QDoubleSpinBox(box)
        self.space_spin.setDecimals(1)
        self.space_spin.setRange(0.0, 10_000_000.0)
        self.space_spin.setSingleStep(10.0)
        self.space_spin.setValue(100.0)
        self.space_spin.setSuffix(" m")
        grid.addWidget(self.space_spin, 0, 1)
        # The accuracy toggle belongs to the distance rule, so it rides that row's tail.
        self.accuracy_check = QCheckBox("Account for recorded accuracy", box)
        grid.addWidget(self.accuracy_check, 0, 3, 1, 3)

        grid.addWidget(QLabel("Time window", box), 1, 0)
        self.time_before_spin = QDoubleSpinBox(box)
        self.time_before_spin.setDecimals(2)
        self.time_before_spin.setRange(0.0, 1_000_000.0)
        self.time_before_spin.setValue(10.0)
        self.time_before_unit_combo = QComboBox(box)
        self.time_before_unit_combo.addItems(DURATION_UNIT_NAMES)
        self.time_before_unit_combo.setCurrentText("minutes")
        grid.addWidget(self.time_before_spin, 1, 1)
        grid.addWidget(self.time_before_unit_combo, 1, 2)
        grid.addWidget(QLabel("before", box), 1, 3)

        self.time_after_spin = QDoubleSpinBox(box)
        self.time_after_spin.setDecimals(2)
        self.time_after_spin.setRange(0.0, 1_000_000.0)
        self.time_after_spin.setValue(30.0)
        self.time_after_unit_combo = QComboBox(box)
        self.time_after_unit_combo.addItems(DURATION_UNIT_NAMES)
        self.time_after_unit_combo.setCurrentText("minutes")
        grid.addWidget(self.time_after_spin, 1, 4)
        grid.addWidget(self.time_after_unit_combo, 1, 5)
        grid.addWidget(QLabel("after", box), 1, 6)
        grid.setColumnStretch(7, 1)   # slack collects on the right, not between controls

        return box

    def _build_results_box(self) -> QGroupBox:
        box = QGroupBox("Results", self.root)
        row = QHBoxLayout(box)
        self.output_edit = QLineEdit(box)
        self.output_edit.setObjectName("outputEdit")
        # Deliberately EMPTY until Case 1 is opened: a path pre-filled at launch points
        # at whatever directory the app happened to start in, which is almost never where
        # the operator wants results. _suggest_output_path fills it from the case instead.
        self.output_edit.setPlaceholderText(
            "chosen automatically when Case 1 is opened; or pick a folder")
        self.output_folder_button = QPushButton("Folder…", box)
        row.addWidget(QLabel("Write to", box))
        row.addWidget(self.output_edit, 1)
        row.addWidget(self.output_folder_button)
        return box

    def _build_action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.clear_log_button = QPushButton("Clear log", self.root)
        self.progress_bar = QProgressBar(self.root)
        # Idle at rest: a 0..0 range animates forever, so an untouched panel looked like it
        # was already working. It only becomes indeterminate once a run actually starts.
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(_PROGRESS_BAR_WIDTH)
        self.status_label = QLabel("Idle", self.root)
        row.addWidget(self.clear_log_button)
        row.addWidget(self.progress_bar)
        row.addWidget(self.status_label)
        row.addStretch(1)
        self.open_error_reports_button = QPushButton(self.root)
        report_actions.sync_report_button(self.open_error_reports_button)
        self.open_output_folder_button = QPushButton("Open output folder", self.root)
        self.open_output_folder_button.setEnabled(False)
        self.run_button = QPushButton("Run", self.root)
        self.run_button.setObjectName("runButton")   # reuses the shared blue-button QSS
        row.addWidget(self.open_error_reports_button)
        row.addWidget(self.open_output_folder_button)
        row.addWidget(self.run_button)
        return row

    def _configure_accessibility(self) -> None:
        self.case_1_edit.setAccessibleName("Case 1 path")
        self.case_1_button.setAccessibleName("Open case 1")
        self.compare_check.setAccessibleName("Compare against a second case")
        self.case_2_edit.setAccessibleName("Case 2 path")
        self.case_2_button.setAccessibleName("Open case 2")
        for widgets, number in ((self._groups[_SIDE_1], "1"), (self._groups[_SIDE_2], "2")):
            widgets.kind_combo.setAccessibleName(f"Group {number} criterion kind")
            widgets.value_combo.setAccessibleName(f"Group {number} criterion value")
            widgets.add_button.setAccessibleName(f"Add criterion to group {number}")
            widgets.chips_list.setAccessibleName(f"Group {number} criteria")
            widgets.chips_list.setAccessibleDescription(
                "The criteria selected for this group; Delete removes the focused one.")
            widgets.any_button.setAccessibleName(f"Group {number} matches any criterion")
            widgets.all_button.setAccessibleName(f"Group {number} matches all criteria")
        self.space_spin.setAccessibleName("Distance threshold in metres")
        self.accuracy_check.setAccessibleName("Account for recorded accuracy")
        self.time_before_spin.setAccessibleName("Time window before, value")
        self.time_before_unit_combo.setAccessibleName("Time window before, unit")
        self.time_after_spin.setAccessibleName("Time window after, value")
        self.time_after_unit_combo.setAccessibleName("Time window after, unit")
        self.output_edit.setAccessibleName("Output CSV path")
        self.output_folder_button.setAccessibleName("Choose output folder")
        self.clear_log_button.setAccessibleName("Clear log")
        self.progress_bar.setAccessibleName("Concomitance run progress")
        self.open_output_folder_button.setAccessibleName("Open output folder")
        self.run_button.setAccessibleName("Run concomitance analysis")
        self.log_edit.setAccessibleName("Spatiotemporal analysis log")

    def _wire_signals(self) -> None:
        self.case_1_button.clicked.connect(lambda: self._open_case(_SIDE_1))
        self.case_2_button.clicked.connect(lambda: self._open_case(_SIDE_2))
        self.compare_check.toggled.connect(self._on_compare_toggled)
        self.output_folder_button.clicked.connect(self._browse_output_folder)
        self.clear_log_button.clicked.connect(self.log_edit.clear)
        self.open_error_reports_button.clicked.connect(self._on_report_button)
        self.open_output_folder_button.clicked.connect(self._open_output_folder)
        self.run_button.clicked.connect(self._on_run_clicked)

        for side, widgets in self._groups.items():
            widgets.kind_combo.currentIndexChanged.connect(
                lambda _index, s=side: self._populate_value_combo(s))
            widgets.add_button.clicked.connect(lambda _checked=False, s=side: self._add_criterion(s))
            widgets.any_button.toggled.connect(
                lambda checked, s=side: self._on_mode_changed(s, checked))
            widgets.chips_list.installEventFilter(self)

    # --- cases --------------------------------------------------------------------------

    def _on_compare_toggled(self, checked: bool) -> None:
        self.case_2_edit.setEnabled(checked)
        self.case_2_button.setEnabled(checked)
        if not checked:
            self._case_2_vocabulary = {}
        self._groups[_SIDE_2].box.setTitle(
            "Group 2 — Case 2" if checked and self.case_2_edit.text().strip()
            else "Group 2 — Case 1")
        self._refresh_group(_SIDE_2)

    def _open_case(self, side: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.root, "Open a case database", str(Path.cwd()),
            "mATLAS case database (*.matlas.sqlite);;All files (*)")
        if not path:
            return
        db_path = Path(path)
        if not is_matlas_run_db(db_path):
            self._show_error(f"Not a mATLAS case database: {db_path.name}")
            return
        try:
            vocabulary = read_case_vocabulary(db_path)
        except Exception as exc:   # noqa: BLE001 - a bad/foreign file is user-facing
            self._show_error(f"Could not read case vocabulary: {exc}")
            return
        if side == _SIDE_1:
            self.case_1_edit.setText(str(db_path))
            self._case_1_vocabulary = vocabulary
            self._suggest_output_path(db_path)
            self._refresh_group(_SIDE_1)
            if not self.compare_check.isChecked():
                self._refresh_group(_SIDE_2)
        else:
            self.case_2_edit.setText(str(db_path))
            self._case_2_vocabulary = vocabulary
            self._groups[_SIDE_2].box.setTitle("Group 2 — Case 2")
            self._refresh_group(_SIDE_2)
        self._append_log(f"Opened case {db_path.name} for {side.replace('_', ' ')}")

    def _suggest_output_path(self, case_db: Path) -> None:
        """Propose a results file beside Case 1, the first time a case is opened.

        WHY only when the field is empty: the suggestion is a convenience, and silently
        rewriting a path the operator typed or picked would lose their choice on the next
        case swap. The results land next to the evidence they came from, which is where
        an analyst looks for them."""
        if self.output_edit.text().strip():
            return
        name = f"concomitance-{dt.date.today().isoformat()}.csv"
        self.output_edit.setText(str(case_db.parent / name))

    def _vocabulary_for(self, side: str) -> dict[CriterionKind, tuple[VocabularyEntry, ...]]:
        if side == _SIDE_2 and self.compare_check.isChecked():
            return self._case_2_vocabulary
        return self._case_1_vocabulary

    # --- group criteria -------------------------------------------------------------------

    def _refresh_group(self, side: str) -> None:
        widgets = self._groups[side]
        widgets.vocabulary = self._vocabulary_for(side)
        self._populate_value_combo(side)

    def _populate_value_combo(self, side: str) -> None:
        widgets = self._groups[side]
        combo = widgets.value_combo
        combo.clear()
        entries = widgets.vocabulary.get(widgets.current_kind(), ())
        for entry in entries:
            qualifier = f" ({entry.qualifier})" if entry.qualifier else ""
            combo.addItem(
                f"{entry.value}{qualifier} · {entry.placement_count} placement(s)",
                entry.value)
        combo.setCurrentIndex(-1)
        combo.clearEditText()

    def _combo_typed_values(self, combo: QComboBox) -> tuple[str, ...]:
        """The value(s) to add: the vocabulary entry's raw value when one is actually
        SELECTED (``currentIndex`` valid), or every token typed/pasted otherwise —
        letting an operator paste several values at once (see ``split_identifiers``).
        """
        if combo.currentIndex() >= 0:
            data = combo.itemData(combo.currentIndex())
            if data is not None:
                return (str(data),)
        return split_identifiers(combo.currentText())

    def _add_criterion(self, side: str) -> None:
        widgets = self._groups[side]
        kind = widgets.current_kind()
        for value in self._combo_typed_values(widgets.value_combo):
            criterion = Criterion(kind=kind, value=value)
            if criterion not in widgets.criteria:   # dedupe: adding the same token twice is a no-op
                widgets.criteria.append(criterion)
        widgets.value_combo.setCurrentIndex(-1)
        widgets.value_combo.clearEditText()
        self._refresh_chips(side)

    def _remove_criterion(self, side: str, criterion: Criterion) -> None:
        widgets = self._groups[side]
        if criterion in widgets.criteria:
            widgets.criteria.remove(criterion)
            self._refresh_chips(side)

    def _on_mode_changed(self, side: str, any_checked: bool) -> None:
        self._groups[side].mode = MatchMode.ANY if any_checked else MatchMode.ALL

    def _chip_label(self, side: str, criterion: Criterion) -> str:
        entries = self._groups[side].vocabulary.get(criterion.kind, ())
        match = next((entry for entry in entries if entry.value == criterion.value), None)
        count = f" · {match.placement_count} placement(s)" if match is not None else ""
        return f"{criterion.value}{count}"

    def _refresh_chips(self, side: str) -> None:
        widgets = self._groups[side]
        widgets.chips_list.clear()
        if not widgets.criteria:
            placeholder = QListWidgetItem("All entities in this case")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            widgets.chips_list.addItem(placeholder)
            return
        for criterion in widgets.criteria:
            # WHY no list-widget parent here: QListWidgetItem(list) ALREADY appends the item
            # (see its docs), so passing widgets.chips_list here AND calling addItem() below
            # would insert the same item twice and corrupt the list's internal state — the
            # explicit addItem() call is the only insertion.
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, criterion)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 2, 6, 2)
            row_layout.addWidget(QLabel(self._chip_label(side, criterion), row), 1)
            remove_button = QToolButton(row)
            remove_button.setText("×")
            remove_button.setAccessibleName(f"Remove {criterion.value}")
            remove_button.clicked.connect(
                lambda _checked=False, s=side, c=criterion: self._remove_criterion(s, c))
            row_layout.addWidget(remove_button)
            item.setSizeHint(row.sizeHint())
            widgets.chips_list.addItem(item)
            widgets.chips_list.setItemWidget(item, row)

    def eventFilter(self, watched: QObject, event: object) -> bool:
        # Delete-key removal (the "x" chip button is a mouse-only affordance).
        if (isinstance(event, QKeyEvent) and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)):
            for side, widgets in self._groups.items():
                if watched is widgets.chips_list:
                    item = widgets.chips_list.currentItem()
                    criterion = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
                    if criterion is not None:
                        self._remove_criterion(side, criterion)
                    return True
        return super().eventFilter(watched, event)

    # --- output path ----------------------------------------------------------------------

    def _browse_output_folder(self) -> None:
        current = Path(self.output_edit.text()).expanduser()
        start_dir = str(current.parent if current.parent.is_dir() else Path.cwd())
        folder = QFileDialog.getExistingDirectory(self.root, "Choose output folder", start_dir)
        if not folder:
            return
        name = current.name or f"concomitance-{dt.date.today().isoformat()}.csv"
        self.output_edit.setText(str(Path(folder) / name))

    # --- running --------------------------------------------------------------------------

    def _build_current_request(self):
        return build_request(
            case_1=self.case_1_edit.text().strip(),
            case_2=self.case_2_edit.text().strip() if self.compare_check.isChecked() else None,
            group_1_criteria=tuple(self._groups[_SIDE_1].criteria),
            group_1_mode=self._groups[_SIDE_1].mode,
            group_2_criteria=tuple(self._groups[_SIDE_2].criteria),
            group_2_mode=self._groups[_SIDE_2].mode,
            space_threshold_m=self.space_spin.value(),
            time_before_value=self.time_before_spin.value(),
            time_before_unit=self.time_before_unit_combo.currentText(),
            time_after_value=self.time_after_spin.value(),
            time_after_unit=self.time_after_unit_combo.currentText(),
            use_accuracy=self.accuracy_check.isChecked(),
            output_path=self.output_edit.text().strip(),
        )

    def _on_run_clicked(self) -> None:
        if self._running:
            self._request_cancel()
            return
        if not self.case_1_edit.text().strip():
            self._show_error("Open Case 1 before running.")
            return
        try:
            request = self._build_current_request()
        except ValueError as exc:   # a bad duration unit — surface it, don't crash
            self._show_error(str(exc))
            return

        problems = check_request(request)
        errors = [p for p in problems if p.severity is Severity.ERROR]
        if errors:
            self._show_error("\n".join(p.message for p in errors))
            return
        warnings = [p for p in problems if p.severity is Severity.WARNING]
        if warnings:
            choice = QMessageBox.warning(
                self.root, "Spatio-temporal concomitance",
                "\n".join(p.message for p in warnings) + "\n\nContinue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Yes:
                return

        self._start_run(request)

    def _start_run(self, request) -> None:
        self._set_running(True)
        self.open_output_folder_button.setEnabled(False)
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        progress = self._progress   # capture for the worker closure (no self on-thread)

        def on_progress(done: int, total: int) -> None:
            progress.progress.emit(done, total)

        def task() -> ConcomitanceRunResult:
            return run_concomitance(
                request, on_progress=on_progress, should_cancel=cancel_event.is_set)

        self._runner.run(task, self._on_run_done, self._on_run_failed)

    def _request_cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self.run_button.setEnabled(False)
        self.run_button.setText("Cancelling…")
        self._append_log("Cancelling…")

    def _on_run_done(self, result: ConcomitanceRunResult) -> None:
        self._last_result = result
        self._set_running(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self._last_output_folder = result.output_path.parent
        self.open_output_folder_button.setEnabled(True)
        summary = f"{result.match_count} match(es) written to {result.output_path.name}"
        self.status_label.setText(summary)
        self._append_log(summary)
        log.info(f"Concomitance run: {summary}")
        for warning in result.warnings:
            self._append_log(f"Warning: {warning.message}")

    def _on_run_failed(self, tb: str) -> None:
        self._set_running(False)
        report_actions.sync_report_button(self.open_error_reports_button)
        last = tb.strip().splitlines()[-1] if tb.strip() else "Concomitance run failed"
        log.error(f"Concomitance run failed:\n{tb}")
        self._append_log(last)
        self._show_error(last)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.run_button.setEnabled(True)
        self.run_button.setText("Cancel" if running else "Run")
        if running:
            self.progress_bar.setRange(0, 0)   # busy until the first anchor total arrives
            self.status_label.setText("Running…")
        else:
            self._cancel_event = None

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        self.status_label.setText(f"{done}/{total}" if total else "Running…")

    # --- misc actions ---------------------------------------------------------------------

    def _on_report_button(self) -> None:
        """Open the error-report folder, or file a bug report — see gui.report_actions.

        Shares that helper with the Transform bar rather than mirroring it: this used to be
        a hand-copied twin of MainController's handler, and the two would have drifted the
        moment one of them learned the bug-report half."""
        report_actions.handle_report_button(
            self.root, self.open_error_reports_button,
            context={"entrypoint": "gui", "tab": "concomitance",
                     "case_1": self.case_1_edit.text().strip() or None,
                     "case_2": self.case_2_edit.text().strip() or None},
        )

    def _open_output_folder(self) -> None:
        if self._last_output_folder is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_output_folder)))

    def _append_log(self, message: str) -> None:
        self.log_edit.append(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self.root, "Spatio-temporal concomitance", message)
