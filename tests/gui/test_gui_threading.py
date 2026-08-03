"""Worker-thread-safety tests for the GUI (gui.db_edit_controller / gui.worker / the shared
TaskRunner).

Defines:    an ASYNC "Open database" test that drives the real off-thread open path and asserts
            the EditApi/sqlite open + widget population land on the GUI thread (so a subsequent
            in-place edit does not raise sqlite3.ProgrammingError), and a shared-runner test that
            a pending spatiotemporal query is waited on at quit (wait_all) without a destroyed
            running QThread.
Used by:    pytest (headless: QT_QPA_PLATFORM=offscreen is forced below).
Depends on: gui.db_edit_controller, gui.spatiotemporal_controller, gui.worker, PySide6, and the
            persisted-DB builder reused from tests.app.test_edit_api.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# Force offscreen BEFORE any Qt import so the suite runs headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QWidget

from gui.db_edit_controller import DbEditController
from gui.spatiotemporal_controller import SpatiotemporalController
from gui.worker import TaskRunner
from tests.app.test_edit_api import _build_persisted_db
from tests.gui.conftest import drain


def _pump_until(app, predicate, timeout: float = 5.0) -> None:
    """Spin the event loop until ``predicate`` holds, so queued cross-thread slots run."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QThread.msleep(5)
    assert predicate(), "timed out waiting for the background task to complete"


def test_async_open_lands_on_gui_thread_and_edit_does_not_raise(app, tmp_path) -> None:
    # The REAL break: with a plain-class controller (or a lambda finished-callback), the async
    # open's callbacks run on the worker thread, so the sqlite/EditApi connection is opened
    # off-thread and every later GUI-thread edit raises sqlite3.ProgrammingError. Drive the
    # async path and prove the open + a subsequent update both happen on the GUI thread.
    path = tmp_path / "case.matlas.sqlite"
    keys = _build_persisted_db(path)
    parent = QWidget()
    runner = TaskRunner(parent)
    controller = DbEditController(parent, runner=runner)
    gui_thread = QThread.currentThread()

    # Spy the editing-connection open (called by the real, queued _apply_opened via a normal
    # Python call, so it runs on whatever thread _apply_opened runs on) — WITHOUT touching the
    # signal connection, so the queued-vs-direct behaviour under test is untouched.
    seen: dict[str, object] = {}
    original_open = controller._open_editing_api

    def spy_open(open_path: Path) -> None:
        seen["thread"] = QThread.currentThread()
        original_open(open_path)

    controller._open_editing_api = spy_open   # type: ignore[method-assign]

    controller._open_async(path)
    _pump_until(app, lambda: "thread" in seen)

    # The EditApi/sqlite open landed on the GUI thread (not the worker thread).
    assert seen["thread"] is gui_thread
    # The navigator populated (widget mutation) on the GUI thread.
    assert controller.navigator.topLevelItemCount() > 0
    # The live editing connection is usable ON THE GUI THREAD: this update would raise
    # sqlite3.ProgrammingError if the connection had been opened on the worker thread.
    si_id = keys["spatial_integration_id"]
    controller._api.update("spatial_integration", si_id, "latitude_wgs84", 46.7)
    assert controller._api.row("spatial_integration", si_id)["latitude_wgs84"] == 46.7

    runner.wait_all()
    controller._api.close()
    parent.deleteLater()
    drain(app)   # run that delete NOW rather than at some later timer tick


def test_pending_panel_query_is_waited_at_quit(app, tmp_path) -> None:
    # The spatiotemporal panel shares the app's lifecycle-managed runner, so wait_all() (wired
    # to aboutToQuit) also drains its concomitance query — quitting mid-query must not leave a
    # running QThread to be destroyed. Kick off a query, then wait_all() as quit would.
    path = tmp_path / "case.matlas.sqlite"
    _build_persisted_db(path)
    parent = QWidget()
    runner = TaskRunner(parent)
    panel = SpatiotemporalController(parent, runner=runner)
    panel.db_edit.setText(str(path))

    panel._run_query()                 # dispatches onto the shared runner
    runner.wait_all()                  # what aboutToQuit does: block on the live worker
    _pump_until(app, lambda: not panel.run_button.text().endswith("…"))

    # The query completed cleanly (a result set was produced) and the panel left the running
    # state — no thread was destroyed while still running.
    assert panel.run_button.isEnabled()
    assert panel._result is not None

    runner.wait_all()
    parent.deleteLater()
    drain(app)   # run that delete NOW rather than at some later timer tick
