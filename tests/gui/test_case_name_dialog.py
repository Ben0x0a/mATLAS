"""Tests for the New case prompt (gui.case_name_dialog) and the drop field it reuses.

Defines:    tests that dropping a file names the case after it (without extension) and keeps
            the dropped path, and that the shared field still writes the full path elsewhere.
Used by:    pytest (offscreen Qt, see tests/gui/conftest.py).
Depends on: gui.case_name_dialog, gui.widgets.drag_drop_line_edit, PySide6.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from gui.case_name_dialog import build_case_name_dialog
from gui.widgets.drag_drop_line_edit import DragDropLineEdit


def _drop(field: DragDropLineEdit, path: Path) -> None:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    field.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def test_dropping_a_file_names_the_case_after_it(app, tmp_path: Path) -> None:
    report = tmp_path / "222104-iPhone_A2221.ufdr"
    report.write_bytes(b"x")
    _dialog, edit = build_case_name_dialog(None, "old name")
    assert edit.text() == "old name"
    _drop(edit, report)
    assert edit.text() == "222104-iPhone_A2221"
    assert edit.dropped == report


def test_the_shared_field_still_writes_the_full_path(app, tmp_path: Path) -> None:
    field = DragDropLineEdit()
    _drop(field, tmp_path / "report.xml")
    assert field.text() == str(tmp_path / "report.xml")
