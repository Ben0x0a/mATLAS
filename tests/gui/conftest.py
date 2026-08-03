"""Shared fixtures for the GUI test modules.

Defines:    ``app`` (the one process-wide QApplication), ``controller`` (a real MainController
            built offscreen and torn down safely) and ``drain`` (force every pending queued
            call AND every pending deleteLater to run now).
Used by:    every module under tests/gui — test_tab_shell, test_case_bar,
            test_transform_interactions (controller), test_db_edit_controller,
            test_spatiotemporal_panel, test_gui_threading (app).
Depends on: gui.app (MainController), PySide6.
"""
from __future__ import annotations

import os
import time

# Force the offscreen platform BEFORE any Qt import, so the suite runs headless (CI, SSH).
# conftest is imported ahead of every test module, so this is the earliest point it can be
# set; setdefault keeps a developer's explicit platform choice intact.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QEvent, QThread
from PySide6.QtWidgets import QApplication

from gui.app import MainController

# How long the controller fixture waits for the construction-time async preset load before
# giving up and running the tests anyway (seconds / poll interval in ms).
_PRESET_LOAD_TIMEOUT_S = 5.0
_PUMP_INTERVAL_MS = 5


@pytest.fixture(scope="module")
def app() -> QApplication:
    """The process-wide QApplication: Qt allows exactly one, so it is created once and reused."""
    return QApplication.instance() or QApplication([])


def drain(app: QApplication) -> None:
    """Run every pending queued call AND every pending deleteLater, now.

    ``processEvents`` deliberately skips ``DeferredDelete``, so an object handed to
    ``deleteLater`` stays alive until some later, arbitrary pass of the event loop. In a test
    suite there is no running loop, so that pass lands wherever Qt happens to dispatch next —
    typically a timer tick long after the fixture returned, with the Python wrappers already
    being collected. Forcing the deletes here makes teardown deterministic instead.
    """
    app.processEvents()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope="module")
def controller(app: QApplication):
    """One real MainController per test module, built offscreen and torn down safely.

    Construction is the expensive part and schedules an async preset load through a queued
    ``singleShot``, so the loop is pumped until the available list fills. That serves two
    purposes: tests reading the preset lists would otherwise race an empty list, and a
    ``singleShot`` still pending at teardown would first fire against deleted widgets.

    The teardown order is why this fixture is shared rather than copied per module:
    ``shutdown()`` silences the timer/bridges, joins the workers and detaches the log handler
    while the widgets are still alive; only then are the window AND the controller destroyed.
    Destroying the controller matters — it parents the run timer, the task runner and the
    log/progress bridges, which otherwise outlive the window and can fire into freed memory.
    """
    ctrl = MainController()
    deadline = time.monotonic() + _PRESET_LOAD_TIMEOUT_S
    while not ctrl.available_preset_list.count() and time.monotonic() < deadline:
        app.processEvents()
        QThread.msleep(_PUMP_INTERVAL_MS)
    yield ctrl
    ctrl.shutdown()
    ctrl.window.deleteLater()
    ctrl.deleteLater()
    drain(app)
