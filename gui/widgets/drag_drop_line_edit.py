"""Drag-and-drop enabled path field.

Defines:    DragDropLineEdit — a QLineEdit accepting one local file/folder drop.
Used by:    gui.main_window.ui (source and presets fields, through gui.app) and
            gui.case_name_dialog (the New case name field).
Depends on: PySide6.

What a drop writes into the field is decided by ``drop_text`` (the full path by default), so a
field can show something else — the New case prompt shows the file's name without extension —
and ``dropped`` keeps the last dropped path for the caller.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QLineEdit, QWidget


class DragDropLineEdit(QLineEdit):
    """QLineEdit that accepts one local file/folder drop."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.dropped: Path | None = None

    def drop_text(self, path: Path) -> str:
        """The text a dropped ``path`` puts in the field (override to change it)."""
        return str(path)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return
        path = urls[0].toLocalFile()
        if not path:
            event.ignore()
            return
        self.dropped = Path(path)
        self.setText(self.drop_text(self.dropped))
        event.acceptProposedAction()
