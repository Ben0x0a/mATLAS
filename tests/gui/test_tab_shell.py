"""Smoke tests for the three-tab GUI shell (gui.app + gui/main_window.ui).

Constructs the real MainController offscreen and checks the QTabWidget shell: three
named tabs in order, the Transform form reparented (not rewritten) into the first tab
with its widgets still bound and reachable, and accessible names on the tab bar and
each page. Placeholder pages must stay minimal: one static label, no machinery.
Used by:    pytest (headless: QT_QPA_PLATFORM=offscreen is forced below).
Depends on: gui.app (MainController), gui/main_window.ui, PySide6.
"""
from __future__ import annotations

import logging
import os

# Force the offscreen platform BEFORE any Qt import, so the suite runs headless (CI,
# SSH). setdefault keeps a developer's explicit platform choice intact.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QApplication, QLabel, QTabWidget

from gui.app import MainController

EXPECTED_TAB_TITLES = ["Transform", "Database Edit", "Spatiotemporal Analysis"]


@pytest.fixture(scope="module")
def controller():
    """One MainController for the whole module: construction is the expensive part and
    the checks below are read-only, so sharing it keeps the suite fast."""
    app = QApplication.instance() or QApplication([])
    ctrl = MainController()
    # Fire the construction-time singleShot (async preset load) NOW, while the window is
    # alive — otherwise it would first fire inside the teardown processEvents below,
    # after the widgets are deleted, and crash on a dead QLabel.
    app.processEvents()
    yield ctrl
    # Teardown order matters: finish worker threads, drain their queued result signals
    # while the widgets still exist, then detach the GUI log handler from the root
    # logger so later (non-GUI) tests never log into a torn-down widget.
    ctrl._runner.wait_all()
    app.processEvents()
    if ctrl._log_handler is not None:
        logging.getLogger().removeHandler(ctrl._log_handler)
    ctrl.window.deleteLater()
    app.processEvents()


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
        controller.output_edit,
        controller.available_preset_list,
        controller.selected_preset_list,
        controller.run_button,
        controller.status_label,
        controller.log_edit,
    ):
        assert transform_page.isAncestorOf(widget)


def test_placeholder_tabs_are_minimal_labelled_pages(controller) -> None:
    for page, label_name in (
        (controller.database_edit_tab, "databaseEditPlaceholderLabel"),
        (controller.spatiotemporal_tab, "spatiotemporalPlaceholderLabel"),
    ):
        label = page.findChild(QLabel, label_name)
        assert label is not None and label.text()
        # Placeholders stay empty shells for T71/T72: exactly one child label, nothing else.
        assert page.findChildren(QLabel) == [label]


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
