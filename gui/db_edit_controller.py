"""Controller for the Database Edit tab: navigate + edit a persisted mATLAS run DB.

Defines:    ``DbEditController`` — builds the Database Edit panel inside a parent QWidget and
            wires its widgets to ``model_atlas.app.edit_api.EditApi``. It owns NO business
            logic: every mutation and every protection decision is delegated to the edit API
            (MVC — the view only calls the model). The panel offers a table/row tree
            navigator, a labelled field editor for the selected row, the three edit actions
            (Save edits / Save as new row / Add empty row) and a read-only edit_log view.
Used by:    the GUI orchestrator (it constructs one ``DbEditController`` passing the
            ``databaseEditTab`` QWidget) and ``tests.gui.test_db_edit_controller``.
Depends on: PySide6 (widgets), ``gui.worker`` (TaskRunner — the long initial DB read runs off
            the GUI thread), ``model_atlas.app.edit_api`` (EditApi + EDITABLE_TABLES — the
            single authority on which tables may be updated in place).

Threading (SQLite affinity)
---------------------------
A ``sqlite3`` connection may only be used on the thread that created it, so the live editing
connection stays on the GUI thread. The one genuinely long operation — reading every table
for the navigator when a DB is opened — runs in a TaskRunner worker that opens its OWN
short-lived EditApi, reads a plain-dict snapshot and closes it, so no connection ever crosses
a thread. The GUI thread then opens its own EditApi for the fast per-edit writes.
``open_database`` performs the same work synchronously for tests and headless callers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from gui.worker import TaskRunner
from model_atlas.app.edit_api import EDITABLE_TABLES, EditApi

# Column headers for the read-only edit_log view (the audit-trail order the model logs in).
_LOG_COLUMNS: tuple[str, ...] = (
    "ordinal", "table_name", "row_id", "column", "old_value", "new_value",
    "edit_kind", "edited_by", "edited_at",
)


class DbEditController:
    """Build and drive the Database Edit panel inside ``parent``.

    The constructor only builds widgets (no DB is opened), so it is safe to construct
    headless with an offscreen parent QWidget; call :meth:`open_database` to load a DB.
    """

    def __init__(self, parent: QWidget, *, runner: TaskRunner | None = None) -> None:
        # The runner drives the one long op (reading all rows). Reuse the caller's if given
        # (so the app shares a single lifecycle-managed runner); otherwise own one parented to
        # the panel so its threads are cleaned up with the widget.
        self._parent = parent
        self._runner = runner or TaskRunner(parent)
        self._api: EditApi | None = None
        self._path: Path | None = None
        self._current_table: str | None = None
        self._current_row_id: str | None = None
        self._field_edits: dict[str, QLineEdit] = {}
        self._build_ui(parent)
        self._sync_action_state()

    # --- construction ------------------------------------------------------

    def _build_ui(self, parent: QWidget) -> None:
        """Assemble the panel; reuse the parent's layout if it already has one."""
        root = parent.layout() or QVBoxLayout(parent)
        panel = QWidget(parent)
        root.addWidget(panel)
        outer = QVBoxLayout(panel)

        # Open-DB row.
        open_row = QHBoxLayout()
        self.path_edit = QLineEdit(panel)
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("Open a persisted <name>.matlas.sqlite database")
        self.open_button = QPushButton("Open database", panel)
        self.open_button.clicked.connect(self._on_open_clicked)
        open_row.addWidget(QLabel("Database", panel))
        open_row.addWidget(self.path_edit, 1)
        open_row.addWidget(self.open_button)
        outer.addLayout(open_row)

        # Navigator (tables -> rows) beside the field editor.
        middle = QHBoxLayout()
        self.navigator = QTreeWidget(panel)
        self.navigator.setHeaderLabels(["Table / row"])
        self.navigator.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.navigator.currentItemChanged.connect(self._on_row_selected)
        middle.addWidget(self.navigator, 1)

        editor_box = QGroupBox("Selected row", panel)
        editor_outer = QVBoxLayout(editor_box)
        self.editor_widget = QWidget(editor_box)
        self.editor_form = QFormLayout(self.editor_widget)
        editor_outer.addWidget(self.editor_widget)
        button_row = QHBoxLayout()
        self.save_button = QPushButton("Save edits", editor_box)
        self.save_button.clicked.connect(self._on_save_edits)
        self.new_row_button = QPushButton("Save as new row", editor_box)
        self.new_row_button.clicked.connect(self._on_save_as_new_row)
        self.empty_row_button = QPushButton("Add empty row", editor_box)
        self.empty_row_button.clicked.connect(self._on_add_empty_row)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.new_row_button)
        button_row.addWidget(self.empty_row_button)
        editor_outer.addLayout(button_row)
        middle.addWidget(editor_box, 2)
        outer.addLayout(middle, 1)

        # Read-only audit journal.
        log_box = QGroupBox("Edit log (audit journal)", panel)
        log_outer = QVBoxLayout(log_box)
        self.log_table = QTableWidget(0, len(_LOG_COLUMNS), log_box)
        self.log_table.setHorizontalHeaderLabels(list(_LOG_COLUMNS))
        self.log_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        log_outer.addWidget(self.log_table)
        outer.addWidget(log_box, 1)

        self._configure_accessibility()

    def _configure_accessibility(self) -> None:
        """Accessible names/descriptions, matching gui.accessibility's conventions."""
        self.path_edit.setAccessibleName("Database path")
        self.path_edit.setAccessibleDescription(
            "Path of the persisted mATLAS run database being edited.")
        self.open_button.setAccessibleName("Open database")
        self.open_button.setAccessibleDescription(
            "Choose a persisted mATLAS run database to navigate and edit.")
        self.navigator.setAccessibleName("Run database rows")
        self.navigator.setAccessibleDescription(
            "Tree of the run database tables; expand a table to select one of its rows.")
        self.save_button.setAccessibleName("Save edits")
        self.new_row_button.setAccessibleName("Save as new row")
        self.new_row_button.setAccessibleDescription(
            "Mint a new occurrence carrying the edited fields, keeping the original row.")
        self.empty_row_button.setAccessibleName("Add empty row")
        self.empty_row_button.setAccessibleDescription(
            "Insert a blank row into the selected table.")
        self.log_table.setAccessibleName("Edit log")
        self.log_table.setAccessibleDescription(
            "Read-only audit journal of every edit, in order.")

    # --- opening a database ------------------------------------------------

    def _on_open_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self._parent, "Open mATLAS run database", "",
            "mATLAS run DB (*.matlas.sqlite);;SQLite (*.sqlite);;All files (*)")
        if path:
            self._open_async(Path(path))

    def _open_async(self, path: Path) -> None:
        """Read the display snapshot off the GUI thread, then open the editing connection.

        The worker opens and closes its OWN EditApi (a connection never crosses a thread);
        on success the GUI thread opens its editing EditApi and populates from the snapshot.
        """
        self.open_button.setEnabled(False)
        self._runner.run(
            lambda: _read_snapshot(path),
            lambda snapshot: self._apply_opened(path, snapshot),
            self._on_open_failed)

    def _apply_opened(self, path: Path, snapshot: dict[str, Any]) -> None:
        self.open_button.setEnabled(True)
        self._open_editing_api(path)
        self._populate(snapshot["tables"], snapshot["log"])

    def _on_open_failed(self, message: str) -> None:
        self.open_button.setEnabled(True)
        QMessageBox.critical(self._parent, "Could not open database", message)

    def open_database(self, path: str | Path) -> None:
        """Synchronously open ``path`` for editing and populate the panel (test/headless).

        Same end state as the async open, but without the worker thread, so a headless test
        can open a DB and assert immediately.
        """
        path = Path(path)
        snapshot = _read_snapshot(path)
        self._open_editing_api(path)
        self._populate(snapshot["tables"], snapshot["log"])

    def _open_editing_api(self, path: Path) -> None:
        if self._api is not None:
            self._api.close()
        self._api = EditApi.open(path)
        self._path = path
        self.path_edit.setText(str(path))

    # --- populating the view ----------------------------------------------

    def _populate(self, tables: dict[str, list[dict[str, Any]]],
                  log: list[dict[str, Any]]) -> None:
        self.navigator.clear()
        for table, rows in tables.items():
            parent_item = QTreeWidgetItem([f"{table} ({len(rows)})"])
            for row in rows:
                pk_value = _first_value(row)
                child = QTreeWidgetItem([_row_summary(row)])
                # Stash (table, row_id) so selecting a leaf knows exactly what to load.
                child.setData(0, Qt.ItemDataRole.UserRole, (table, pk_value))
                parent_item.addChild(child)
            self.navigator.addTopLevelItem(parent_item)
        self._refresh_log(log)
        self._clear_editor()

    def _refresh_from_api(self) -> None:
        """Re-read the whole snapshot through the live editing API (after a mutation)."""
        assert self._api is not None
        tables = {t: self._api.rows(t) for t in self._api.tables()}
        self._populate(tables, self._api.edit_log())

    def _refresh_log(self, log: list[dict[str, Any]]) -> None:
        self.log_table.setRowCount(len(log))
        for r, entry in enumerate(log):
            for c, column in enumerate(_LOG_COLUMNS):
                value = entry.get(column)
                self.log_table.setItem(r, c, QTableWidgetItem(
                    "" if value is None else str(value)))

    # --- row selection + field editor -------------------------------------

    def _on_row_selected(self, current: QTreeWidgetItem | None,
                         _previous: QTreeWidgetItem | None) -> None:
        data = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        if data is None or self._api is None:
            self._clear_editor()
            return
        table, row_id = data
        row = self._api.row(table, row_id)
        if row is None:
            self._clear_editor()
            return
        self._current_table, self._current_row_id = table, row_id
        self._load_fields(row)
        self._sync_action_state()

    def _load_fields(self, row: dict[str, Any]) -> None:
        self._clear_form()
        for column, value in row.items():
            edit = QLineEdit(self.editor_widget)
            edit.setText("" if value is None else str(value))
            edit.setAccessibleName(column)
            self.editor_form.addRow(QLabel(column, self.editor_widget), edit)
            self._field_edits[column] = edit

    def _clear_form(self) -> None:
        while self.editor_form.rowCount():
            self.editor_form.removeRow(0)
        self._field_edits = {}

    def _clear_editor(self) -> None:
        self._clear_form()
        self._current_table = None
        self._current_row_id = None
        self._sync_action_state()

    def _sync_action_state(self) -> None:
        """Enable/disable the action buttons; enforce the evidence tooltip (D34)."""
        has_row = self._current_table is not None and self._current_row_id is not None
        editable = has_row and self._current_table in EDITABLE_TABLES
        self.save_button.setEnabled(bool(editable))
        # On an evidence row, Save-edits is disabled and points at the allowed path.
        if has_row and not editable:
            self.save_button.setToolTip(
                "This is evidence and cannot be edited in place — "
                "use 'Save as new row' to record a new occurrence.")
        else:
            self.save_button.setToolTip("")
        self.new_row_button.setEnabled(bool(has_row))
        self.empty_row_button.setEnabled(bool(has_row))

    # --- edit actions (all delegated to the model) ------------------------

    def _on_save_edits(self) -> None:
        if self._api is None or self._current_table is None:
            return
        table, row_id = self._current_table, self._current_row_id
        original = self._api.row(table, row_id) or {}
        try:
            for column, edit in self._field_edits.items():
                new_value = _coerce(edit.text(), original.get(column))
                if new_value != original.get(column):
                    self._api.update(table, row_id, column, new_value)
        except (PermissionError, ValueError, KeyError) as exc:
            QMessageBox.warning(self._parent, "Edit rejected", str(exc))
        self._refresh_from_api()

    def _on_save_as_new_row(self) -> None:
        if self._api is None or self._current_table is None:
            return
        table, row_id = self._current_table, self._current_row_id
        original = self._api.row(table, row_id) or {}
        pk = self._api._pk(table)
        edits = {column: _coerce(edit.text(), original.get(column))
                 for column, edit in self._field_edits.items() if column != pk}
        try:
            self._api.save_as_new_row(table, row_id, edits)
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self._parent, "Could not add row", str(exc))
        self._refresh_from_api()

    def _on_add_empty_row(self) -> None:
        if self._api is None or self._current_table is None:
            return
        try:
            self._api.add_empty_row(self._current_table)
        except ValueError as exc:
            QMessageBox.warning(self._parent, "Could not add row", str(exc))
        self._refresh_from_api()


# --- pure helpers (no Qt / no self — unit-testable) ------------------------

def _read_snapshot(path: Path) -> dict[str, Any]:
    """Open ``path``, read every table + the edit_log into plain dicts, and close.

    A standalone function (opens its OWN EditApi) so it runs safely in a worker thread: the
    connection is created and closed inside the one call, never handed to another thread.
    """
    with EditApi.open(path) as api:
        tables = {table: api.rows(table) for table in api.tables()}
        return {"tables": tables, "log": api.edit_log()}


def _first_value(row: dict[str, Any]) -> Any:
    """The row's primary-key value (the first column — pk leads every model table)."""
    return next(iter(row.values()))


def _row_summary(row: dict[str, Any]) -> str:
    """A one-line label for a row: its id plus the next couple of columns, truncated."""
    items = list(row.items())
    parts = [str(items[0][1])]
    for column, value in items[1:3]:
        if value is not None:
            parts.append(f"{column}={value}")
    return " · ".join(parts)


def _coerce(text: str, original: Any) -> Any:
    """Turn edited field text back into the original value's type.

    HOW: empty text -> None; otherwise cast to the original's type (int/float) so an integer
    or REAL column keeps its storage class, falling back to the raw string when the original
    was text or None. WHY match the original type: the edit API restores exact typed values
    on rewind, so a float column edited to "46.2" must store 46.2, not the string "46.2".
    """
    if text == "":
        return None
    if isinstance(original, bool):
        return text.strip().lower() in ("1", "true", "yes")
    if isinstance(original, int):
        try:
            return int(text)
        except ValueError:
            return text
    if isinstance(original, float):
        try:
            return float(text)
        except ValueError:
            return text
    return text
