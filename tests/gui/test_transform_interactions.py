"""Tests for the Transform page's interaction behaviour (gui.app + gui.widgets).

Covers the three things a user does repeatedly on this page and that Qt does not give for
free: picking several export formats without the menu closing between ticks, moving more
than one preset at a time, and the header filmstrip following the UFDR-dump mode switch.
Used by:    pytest (the shared ``controller`` fixture — which also drains the async preset load
            these tests depend on — and the headless offscreen platform both come from
            tests/gui/conftest.py).
Depends on: gui.app (MainController), gui.widgets.checkable_menu, PySide6.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QAbstractItemView, QLabel


# --- the Additional export menu stays open across ticks -----------------------

def test_toggling_a_format_keeps_the_menu_open(controller) -> None:
    menu = controller.additional_export_button.menu()
    actions = [action for action in menu.actions() if action.isCheckable()]
    assert len(actions) >= 2, "need at least two exporters to make this meaningful"
    target = actions[1]
    menu.setActiveAction(target)
    before = target.isChecked()
    try:
        # Handling the activation itself is what keeps the menu open: returning True means
        # the event never reaches Qt's close-on-trigger path.
        assert menu._toggle_active_action() is True
        assert target.isChecked() is not before
        # ... and it toggles back, so the menu supports deselecting in place too.
        assert menu._toggle_active_action() is True
        assert target.isChecked() is before
    finally:
        target.setChecked(before)
        menu.setActiveAction(None)


def test_menu_defers_to_qt_when_no_checkable_action_is_active(controller) -> None:
    # With nothing highlighted the menu must behave like any other: fall through to Qt so
    # a click outside the items still dismisses it.
    menu = controller.additional_export_button.menu()
    menu.setActiveAction(None)
    assert menu._toggle_active_action() is False


# --- multiple presets move in one go -----------------------------------------

def test_preset_lists_allow_multiple_selection(controller) -> None:
    for list_widget in (controller.available_preset_list, controller.selected_preset_list):
        assert list_widget.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection


def test_adding_several_highlighted_presets_moves_all_of_them(controller) -> None:
    available = controller.available_preset_list
    assert available.count() >= 3, "fixture presets missing"
    controller.selected_preset_list.clear()
    try:
        for row in range(3):
            available.item(row).setSelected(True)
        controller._add_selected_presets()
        assert controller.selected_preset_list.count() == 3
    finally:
        controller._remove_all_presets()
        controller.auto_preset_check.setChecked(True)


# --- the header filmstrip tracks UFDR dump mode -------------------------------

def _header_visibility(controller) -> list[bool]:
    # isVisibleTo(window), not isVisible(): it reports whether the widget would show if its
    # window were shown, so the test never has to show a top-level. Showing one and then
    # deleteLater()-ing it at teardown races Qt's timer dispatch and can crash the whole
    # session — which is why no GUI fixture here shows its window.
    return [
        controller.window.findChild(QLabel, f"headerImage{number}").isVisibleTo(controller.window)
        for number in range(2, 8)
    ]


def test_header_frames_hide_in_ufdr_dump_mode(controller) -> None:
    check = controller.dump_full_ufdr_check
    try:
        assert all(_header_visibility(controller))
        check.setChecked(True)
        # The frames illustrate the transform pipeline, which dump mode does not run.
        assert not any(_header_visibility(controller))
        check.setChecked(False)
        assert all(_header_visibility(controller))
    finally:
        check.setChecked(False)
