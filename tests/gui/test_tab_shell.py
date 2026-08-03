"""Smoke tests for the three-tab GUI shell (gui.app + gui/main_window.ui).

Constructs the real MainController offscreen and checks the QTabWidget shell: three
named tabs in order, the Transform form reparented (not rewritten) into the first tab
with its widgets still bound and reachable, and accessible names on the tab bar and
each page. The other two tabs now hold the Database Edit and Spatiotemporal Analysis
panels (their placeholder labels removed so nothing renders above the real panel).
Used by:    pytest (the shared ``controller`` fixture and the headless offscreen platform both
            come from tests/gui/conftest.py).
Depends on: gui.app (MainController), gui/main_window.ui, PySide6.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QLabel, QTabWidget, QWidget

from gui.app import APP_TITLE_DEFAULT, APP_TITLE_UFDR

EXPECTED_TAB_TITLES = ["Transform", "Database Edit", "Spatiotemporal Analysis"]


def test_window_has_three_named_tabs(controller) -> None:
    tab_widget = controller.window.findChild(QTabWidget, "mainTabWidget")
    assert tab_widget is not None
    assert tab_widget is controller.tab_widget
    titles = [tab_widget.tabText(i) for i in range(tab_widget.count())]
    assert titles == EXPECTED_TAB_TITLES
    assert tab_widget.currentIndex() == 0


def test_transform_form_lives_inside_first_tab(controller) -> None:
    # The reparent must leave every Transform widget bound and physically inside the
    # first tab page — a missing binding would have raised in _bind_widgets already.
    transform_page = controller.tab_widget.widget(0)
    assert transform_page is controller.transform_tab
    for widget in (
        controller.source_edit,
        controller.presets_path_edit,
        controller.available_preset_list,
        controller.selected_preset_list,
        controller.run_button,
        controller.status_label,
        controller.log_edit,
    ):
        assert transform_page.isAncestorOf(widget)


def test_panels_replace_placeholders_in_their_tabs(controller) -> None:
    # T71/T72 now fill the two once-placeholder tabs: the controllers are attached and
    # each tab holds its real panel (child widgets), with T70's scaffolding label removed
    # so it can never render above the panel.
    assert controller._db_edit_controller is not None
    assert controller._spatiotemporal_controller is not None
    for page, placeholder_name in (
        (controller.database_edit_tab, "databaseEditPlaceholderLabel"),
        (controller.spatiotemporal_tab, "spatiotemporalPlaceholderLabel"),
    ):
        assert page.findChild(QLabel, placeholder_name) is None
        assert page.findChildren(QWidget)  # the panel was built inside the tab


def test_tab_shell_accessibility_registered(controller) -> None:
    assert controller.tab_widget.accessibleName() == "Main sections"
    assert controller.tab_widget.tabBar().accessibleName() == "Main section tabs"
    for page in (
        controller.transform_tab,
        controller.database_edit_tab,
        controller.spatiotemporal_tab,
    ):
        assert page.accessibleName()
        assert page.accessibleDescription()


# --- the UFDR dump toggle (now available) ------------------------------------

def test_ufdr_toggle_available_and_switches_title(controller) -> None:
    check = controller.dump_full_ufdr_check
    # Available: enabled and not explicitly hidden (isHidden is independent of whether the
    # top window has been shown, which it is not in these offscreen tests).
    assert check.isEnabled()
    assert not check.isHidden()
    try:
        check.setChecked(True)      # emits toggled -> _sync_ufdr_mode
        assert controller.app_title_label.text() == APP_TITLE_UFDR
        # dump mode greys out the transform-only controls; source/output/run stay live.
        assert not controller.concerns_edit.isEnabled()
        assert not controller.available_preset_list.isEnabled()
        assert not controller.additional_export_button.isEnabled()
        assert controller.source_edit.isEnabled()
        assert controller.run_button.isEnabled()
        check.setChecked(False)
        assert controller.app_title_label.text() == APP_TITLE_DEFAULT
        assert controller.concerns_edit.isEnabled()      # re-enabled off dump mode
        assert controller.available_preset_list.isEnabled()
    finally:
        check.setChecked(False)     # module-scoped controller: leave it as found


def test_run_routes_to_ufdr_dump_when_checked(controller, monkeypatch) -> None:
    # With the box checked, Run invokes the standalone UFDR dump, not the transform pipeline.
    called: list[bool] = []
    monkeypatch.setattr(controller, "_run_ufdr_dump", lambda: called.append(True))
    controller.dump_full_ufdr_check.setChecked(True)
    try:
        controller._run_process()
        assert called == [True]
    finally:
        controller.dump_full_ufdr_check.setChecked(False)
