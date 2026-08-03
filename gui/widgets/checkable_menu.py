"""Menu of check boxes that stays open while items are toggled.

Defines:    CheckableMenu, a QMenu that keeps itself open when a checkable action is
            activated, and closes normally for everything else.
Used by:    gui.app (the 'Additional export' button's format menu).
Depends on: PySide6 QtWidgets/QtCore only.

Qt closes a menu as soon as any action is triggered, which is right for a menu of commands
and wrong for one of independent check boxes: picking three export formats meant reopening
the menu three times, and the list of what was already ticked disappeared at the moment the
user needed to see it. Both the mouse and the keyboard paths are handled, so the menu does
not behave differently depending on how it is driven.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QMenu

_ACTIVATE_KEYS = frozenset({Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter})


class CheckableMenu(QMenu):
    """QMenu that survives toggling one of its checkable actions."""

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if self._toggle_active_action():
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in _ACTIVATE_KEYS and self._toggle_active_action():
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_active_action(self) -> bool:
        """Toggle the highlighted action in place; return whether the menu handled it.

        Only enabled checkable actions are intercepted — a separator, a disabled entry or a
        plain command still falls through to Qt and closes the menu as the user expects.
        """
        action = self.activeAction()
        if action is None or action.isSeparator():
            return False
        if not action.isEnabled() or not action.isCheckable():
            return False
        action.trigger()        # flips the check state and emits toggled/triggered
        return True
