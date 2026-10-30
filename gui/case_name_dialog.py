"""The "New case" name prompt: type a name, or drop a file to name the case after it.

Defines:    CaseNameEdit, build_case_name_dialog (unexecuted — what the tests drive) and
            ask_case_name (shows it).
Used by:    gui.app (MainController._new_case).
Depends on: gui.widgets.drag_drop_line_edit (the drop-aware field), PySide6.

The field is the app's DragDropLineEdit, so a drop behaves like on the source field; only the
text differs: the dropped file's name without its last extension (``iPhone_A2221.ufdr`` ->
``iPhone_A2221``). The dropped path is kept so the caller can open the folder dialog there.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from gui.widgets.drag_drop_line_edit import DragDropLineEdit

_PROMPT = "Case name (or drop a file here to name the case after it):"


class CaseNameEdit(DragDropLineEdit):
    """A dropped file gives the case its name: the file name without its last extension."""

    def drop_text(self, path: Path) -> str:
        return path.stem


def build_case_name_dialog(parent: QWidget | None, current: str) -> tuple[QDialog, CaseNameEdit]:
    """The New case prompt and its name field, ready to exec."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("New case")
    edit = CaseNameEdit(dialog)
    edit.setText(current)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout = QVBoxLayout(dialog)
    label = QLabel(_PROMPT, dialog)
    label.setBuddy(edit)
    for widget in (label, edit, buttons):
        layout.addWidget(widget)
    return dialog, edit


def ask_case_name(parent: QWidget | None, current: str) -> tuple[str | None, Path | None]:
    """Ask for a case name: (the name, or None if cancelled; the dropped path, if any)."""
    dialog, edit = build_case_name_dialog(parent, current)
    if not dialog.exec():
        return None, None
    return edit.text(), edit.dropped
