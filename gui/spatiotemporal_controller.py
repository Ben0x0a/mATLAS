"""The Spatiotemporal Analysis panel controller (T72): who was together, where/when.

Defines:    ``SpatiotemporalController`` — a controller that builds the concomitance
            panel INSIDE a parent QWidget (the "Spatiotemporal Analysis" tab). It
            collects an entity selection (type / identifier_type / an explicit
            identifier list) and the two thresholds, runs
            ``model_atlas.analyze.concomitance.find_concomitant`` OFF-THREAD via the
            shared ``TaskRunner``, shows the pairs (entity A, entity B, distance, time
            gap, and both source assertion ids) in a ``QTableView``, and exports the
            result set to CSV via the T41 ``ResultSet.to_csv`` helper. Map/visualisation
            is deferred (a disabled "Visualise" affordance — see the TODO). Also defines
            the read-only ``_PersistedRunView`` (``open_persisted_run_view``) the engine
            reads through, and the private ``_ConcomitanceTableModel``.
Used by:    the GUI orchestrator, which constructs one ``SpatiotemporalController`` with
            the Spatiotemporal Analysis tab widget as its parent (this file does NOT
            edit gui.app); tests/gui/test_spatiotemporal_panel.py.
Depends on: gui.spatiotemporal_query (the two Qt-free mapping seams), gui.worker
            (TaskRunner / ProgressBridge — reused, never edited), model_atlas.analyze.
            concomitance (find_concomitant / ResultSet), model_atlas.core.vocabularies
            (the valid selection tokens), PySide6, and the standard library (sqlite3).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from gui.spatiotemporal_query import (
    DURATION_UNIT_NAMES,
    RESULT_COLUMNS,
    ConcomitanceRow,
    build_query,
    result_to_rows,
)
from gui.worker import ProgressBridge, TaskRunner
from model_atlas.analyze.concomitance import ResultSet, find_concomitant
from model_atlas.core import vocabularies as V

log = logging.getLogger("model_atlas.gui")


# --- the read-only run-DB view the engine reads through -----------------------

class _PersistedRunView:
    """A read-only ``.connection`` view of a persisted ``<name>.matlas.sqlite``.

    ``find_concomitant`` only issues SELECTs through ``store.connection`` and never
    calls a write method (see its module docstring), so a plain read-only sqlite3
    connection wrapped to expose ``.connection`` is a complete, minimal store view.
    WHY this instead of the full ``RunStore``: opening ``mode=ro`` guarantees a query
    can never mutate the evidence database, and it keeps the panel INDEPENDENT of the
    persisted-DB opener a sibling task is still deciding — we rely only on the single
    attribute the engine documents that it reads.
    """

    def __init__(self, path: str | Path) -> None:
        # URI form with mode=ro: the file must exist AND is opened read-only, so the
        # connection physically cannot write to the persisted evidence DB.
        self._connection = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()


def open_persisted_run_view(path: str | Path) -> _PersistedRunView:
    """Open ``path`` read-only as a store view ``find_concomitant`` accepts."""
    return _PersistedRunView(path)


# --- the results table model --------------------------------------------------

class _ConcomitanceTableModel(QAbstractTableModel):
    """A thin table model over pre-formatted ``ConcomitanceRow`` cells.

    All formatting is done by the Qt-free ``result_to_rows`` seam, so this model only
    serves already-built strings — keeping the display logic testable without Qt.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[ConcomitanceRow] = []

    def set_rows(self, rows: list[ConcomitanceRow]) -> None:
        # A full reset (not per-cell signals): the whole result is replaced at once.
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(RESULT_COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._rows[index.row()].as_cells()[index.column()]

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return RESULT_COLUMNS[section]
        return section + 1


# --- the panel controller -----------------------------------------------------

class SpatiotemporalController(QObject):
    """Build and drive the concomitance panel inside a parent tab widget.

    Construct with the Spatiotemporal Analysis tab as ``parent_tab``; the controller
    lays its panel out inside that widget (reusing an existing layout or creating a
    QVBoxLayout on it). The orchestrator wires this in AFTER building the window — this
    file never edits gui.app / main_window.ui / accessibility.py.
    """

    def __init__(self, parent_tab: QWidget) -> None:
        # Parent this QObject to the tab so its lifetime (and the TaskRunner's threads)
        # is tied to the tab widget's.
        super().__init__(parent_tab)
        self._parent_tab = parent_tab
        self._runner = TaskRunner(self)
        # Progress reporting: the worker emits phase names through this thread-safe
        # bridge; a queued connection marshals each onto the GUI thread (see gui.worker).
        self._progress = ProgressBridge(self)
        self._progress.phase.connect(self._on_phase)
        self._model = _ConcomitanceTableModel(self)
        self._result: ResultSet | None = None

        self._build_ui()
        self._configure_accessibility()
        self._wire_signals()

    # --- construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        # Build everything inside one root widget, then attach it to the tab's layout
        # (creating one if the tab has none) so the panel drops cleanly into the shell.
        self.root = QWidget(self._parent_tab)
        outer = QVBoxLayout(self.root)

        # Run database picker.
        db_row = QHBoxLayout()
        self.db_edit = QLineEdit(self.root)
        self.db_edit.setObjectName("spatiotemporalDbEdit")
        self.db_edit.setPlaceholderText("path to a persisted <name>.matlas.sqlite")
        self.db_button = QPushButton("Browse…", self.root)
        self.db_button.setObjectName("spatiotemporalDbButton")
        db_row.addWidget(QLabel("Run database", self.root))
        db_row.addWidget(self.db_edit, 1)
        db_row.addWidget(self.db_button)
        outer.addLayout(db_row)

        # Entity selection: two vocabulary lists (valid tokens) + the explicit list.
        selection_box = QGroupBox("Entity selection", self.root)
        selection_layout = QHBoxLayout(selection_box)
        self.types_list = self._vocabulary_list(
            "spatiotemporalTypesList", [member.value for member in V.EntityType])
        self.identifier_types_list = self._vocabulary_list(
            "spatiotemporalIdentifierTypesList",
            [member.value for member in V.IdentifierType])
        selection_layout.addLayout(
            self._labelled_column("Entity types", self.types_list))
        selection_layout.addLayout(
            self._labelled_column("Identifier types", self.identifier_types_list))
        identifiers_column = QVBoxLayout()
        identifiers_column.addWidget(QLabel("Identifiers", self.root))
        self.identifiers_edit = QLineEdit(self.root)
        self.identifiers_edit.setObjectName("spatiotemporalIdentifiersEdit")
        self.identifiers_edit.setPlaceholderText(
            "explicit normalised identifiers, comma or newline separated")
        identifiers_column.addWidget(self.identifiers_edit)
        identifiers_column.addStretch(1)
        selection_layout.addLayout(identifiers_column, 1)
        outer.addWidget(selection_box)

        # Thresholds.
        thresholds_box = QGroupBox("Thresholds", self.root)
        thresholds_form = QFormLayout(thresholds_box)
        self.space_spin = QDoubleSpinBox(self.root)
        self.space_spin.setObjectName("spatiotemporalSpaceSpin")
        self.space_spin.setDecimals(1)
        self.space_spin.setRange(0.0, 10_000_000.0)   # 0 m .. 10 000 km
        self.space_spin.setSingleStep(10.0)
        self.space_spin.setValue(100.0)
        self.space_spin.setSuffix(" m")
        thresholds_form.addRow("Spatial threshold", self.space_spin)
        time_row = QHBoxLayout()
        self.time_spin = QDoubleSpinBox(self.root)
        self.time_spin.setObjectName("spatiotemporalTimeSpin")
        self.time_spin.setDecimals(2)
        self.time_spin.setRange(0.0, 1_000_000.0)
        self.time_spin.setValue(10.0)
        self.time_unit_combo = QComboBox(self.root)
        self.time_unit_combo.setObjectName("spatiotemporalTimeUnitCombo")
        self.time_unit_combo.addItems(DURATION_UNIT_NAMES)
        self.time_unit_combo.setCurrentText("minutes")
        time_row.addWidget(self.time_spin, 1)
        time_row.addWidget(self.time_unit_combo)
        thresholds_form.addRow("Temporal threshold", time_row)
        outer.addWidget(thresholds_box)

        # Actions.
        actions_row = QHBoxLayout()
        self.run_button = QPushButton("Find concomitant", self.root)
        self.run_button.setObjectName("spatiotemporalRunButton")
        self.export_button = QPushButton("Export CSV…", self.root)
        self.export_button.setObjectName("spatiotemporalExportButton")
        self.export_button.setEnabled(False)   # nothing to export until a query runs
        # DEFERRED (T72 "talk when time comes"): the map/visualisation is not built.
        # This disabled affordance is the marked hook — TODO(T72 follow-up): wire a map
        # view that plots the concomitant pairs; keep it disabled until then.
        self.visualise_button = QPushButton("Visualise", self.root)
        self.visualise_button.setObjectName("spatiotemporalVisualiseButton")
        self.visualise_button.setEnabled(False)
        self.visualise_button.setToolTip("Map visualisation is deferred (T72).")
        actions_row.addWidget(self.run_button)
        actions_row.addWidget(self.export_button)
        actions_row.addWidget(self.visualise_button)
        actions_row.addStretch(1)
        self.status_label = QLabel("Idle", self.root)
        self.status_label.setObjectName("spatiotemporalStatusLabel")
        actions_row.addWidget(self.status_label)
        outer.addLayout(actions_row)

        # Results table.
        self.results_table = QTableView(self.root)
        self.results_table.setObjectName("spatiotemporalResultsTable")
        self.results_table.setModel(self._model)
        self.results_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self.results_table, 1)

        # Attach the panel to the tab: reuse its layout if the shell gave it one,
        # otherwise own a fresh vertical layout on it.
        layout = self._parent_tab.layout()
        if layout is None:
            layout = QVBoxLayout(self._parent_tab)
        layout.addWidget(self.root)

    def _vocabulary_list(self, object_name: str, tokens: list[str]) -> QListWidget:
        """A multi-select list pre-filled with the valid vocabulary tokens.

        Populating from the vocabulary (never a hand-typed copy) means the offered
        tokens can never drift from what ``find_concomitant`` accepts.
        """
        widget = QListWidget(self.root)
        widget.setObjectName(object_name)
        widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        widget.addItems(tokens)
        return widget

    def _labelled_column(self, text: str, widget: QWidget) -> QVBoxLayout:
        column = QVBoxLayout()
        column.addWidget(QLabel(text, self.root))
        column.addWidget(widget)
        return column

    def _configure_accessibility(self) -> None:
        """Accessible names/descriptions + keyboard tab order (per gui.accessibility).

        Follows the same convention as the Transform form: every interactive widget
        gets an accessible name and, where useful, a description; the focus order runs
        top-to-bottom through the panel so keyboard users traverse it predictably.
        """
        self.db_edit.setAccessibleName("Run database path")
        self.db_edit.setAccessibleDescription(
            "Path to a persisted mATLAS run database to query for concomitance.")
        self.db_button.setAccessibleName("Browse for run database")
        self.types_list.setAccessibleName("Entity types")
        self.types_list.setAccessibleDescription(
            "Restrict the query to entities of the selected types; select none for no "
            "type constraint.")
        self.identifier_types_list.setAccessibleName("Identifier types")
        self.identifier_types_list.setAccessibleDescription(
            "Restrict the query to entities with the selected identifier types; select "
            "none for no constraint.")
        self.identifiers_edit.setAccessibleName("Identifiers")
        self.identifiers_edit.setAccessibleDescription(
            "Explicit list of normalised identifiers, comma or newline separated.")
        self.space_spin.setAccessibleName("Spatial threshold in metres")
        self.space_spin.setAccessibleDescription(
            "Maximum great-circle distance between two placements, in metres.")
        self.time_spin.setAccessibleName("Temporal threshold value")
        self.time_unit_combo.setAccessibleName("Temporal threshold unit")
        self.time_unit_combo.setAccessibleDescription(
            "Unit for the temporal threshold: seconds, minutes, hours, or days.")
        self.run_button.setAccessibleName("Find concomitant pairs")
        self.run_button.setAccessibleDescription(
            "Run the concomitance query on the chosen run database.")
        self.export_button.setAccessibleName("Export results to CSV")
        self.visualise_button.setAccessibleName("Visualise results")
        self.visualise_button.setAccessibleDescription(
            "Map visualisation of the concomitant pairs; deferred and not yet available.")
        self.status_label.setAccessibleName("Spatiotemporal query status")
        self.results_table.setAccessibleName("Concomitant pairs")
        self.results_table.setAccessibleDescription(
            "Table of concomitant assertion pairs: entity A, entity B, distance, time "
            "gap, and the two source assertion ids.")
        for first, second in (
            (self.db_edit, self.db_button),
            (self.db_button, self.types_list),
            (self.types_list, self.identifier_types_list),
            (self.identifier_types_list, self.identifiers_edit),
            (self.identifiers_edit, self.space_spin),
            (self.space_spin, self.time_spin),
            (self.time_spin, self.time_unit_combo),
            (self.time_unit_combo, self.run_button),
            (self.run_button, self.export_button),
            (self.export_button, self.visualise_button),
            (self.visualise_button, self.results_table),
        ):
            QWidget.setTabOrder(first, second)

    def _wire_signals(self) -> None:
        self.db_button.clicked.connect(self._browse_db)
        self.run_button.clicked.connect(self._run_query)
        self.export_button.clicked.connect(self._export)

    # --- actions --------------------------------------------------------------

    def _browse_db(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.root,
            "Choose a persisted run database",
            self.db_edit.text() or str(Path.cwd()),
            "mATLAS run database (*.sqlite *.matlas.sqlite);;All files (*)",
        )
        if path:
            self.db_edit.setText(path)

    def _selected_tokens(self, list_widget: QListWidget) -> list[str]:
        return [item.text() for item in list_widget.selectedItems()]

    def _run_query(self) -> None:
        db_text = self.db_edit.text().strip()
        if not db_text:
            self._show_error("Choose a run database (.matlas.sqlite) to query.")
            return
        db_path = Path(db_text).expanduser()
        if not db_path.is_file():
            self._show_error(f"Run database not found: {db_path}")
            return
        try:
            query = build_query(
                types=self._selected_tokens(self.types_list),
                identifier_types=self._selected_tokens(self.identifier_types_list),
                identifiers_text=self.identifiers_edit.text(),
                space_threshold_m=self.space_spin.value(),
                time_value=self.time_spin.value(),
                time_unit=self.time_unit_combo.currentText(),
            )
        except ValueError as exc:   # a bad duration unit — surface it, don't crash
            self._show_error(str(exc))
            return

        self._set_running(True)
        progress = self._progress   # capture for the worker closure (no self on-thread)

        def task() -> ResultSet:
            # Open, query and CLOSE the read-only connection entirely on the worker
            # thread: sqlite connections are thread-affine, so the whole lifecycle
            # stays off the GUI thread. find_concomitant validates the thresholds and
            # the vocabulary tokens, raising on anything invalid.
            progress.phase.emit("Opening run database")
            view = open_persisted_run_view(db_path)
            try:
                progress.phase.emit("Running concomitance query")
                return find_concomitant(view, **query.kwargs())
            finally:
                view.close()

        self._runner.run(task, self._on_query_done, self._on_query_failed)

    def _on_query_done(self, result: ResultSet) -> None:
        self._result = result
        self._model.set_rows(result_to_rows(result))
        self.export_button.setEnabled(bool(result.pairs))
        self._set_running(False)
        summary = (f"{len(result.pairs)} pair(s) from {result.candidate_count} "
                   f"candidate(s)")
        self.status_label.setText(summary)
        log.info(f"Spatiotemporal query: {summary}")

    def _on_query_failed(self, tb: str) -> None:
        self._set_running(False)
        self.status_label.setText("Query failed")
        last = tb.strip().splitlines()[-1] if tb.strip() else "Query failed"
        log.error(f"Spatiotemporal query failed:\n{tb}")
        self._show_error(last)

    def _export(self) -> None:
        if self._result is None or not self._result.pairs:
            self._show_error("Run a query with results before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.root,
            "Export concomitance results",
            str(Path.cwd() / "concomitant.csv"),
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        try:
            written = self._result.to_csv(Path(path))
        except OSError as exc:   # a write failure is user-facing, not a crash
            self._show_error(f"Could not write CSV: {exc}")
            return
        self.status_label.setText(
            f"Exported {len(self._result.pairs)} pair(s) to {written.name}")
        log.info(f"Spatiotemporal export: {len(self._result.pairs)} pair(s) -> {written}")

    def _set_running(self, running: bool) -> None:
        # Guard against a second run while one is in flight; also flip export off
        # until a fresh result arrives.
        self.run_button.setEnabled(not running)
        self.run_button.setText("Running…" if running else "Find concomitant")
        if running:
            self.export_button.setEnabled(False)
            self.status_label.setText("Running…")

    def _on_phase(self, name: str) -> None:
        self.status_label.setText(name)

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self.root, "Spatiotemporal Analysis", message)
