"""Background-thread plumbing and the GUI log handler for the PySide6 GUI.

Defines:    ProgressBridge (thread-safe per-phase signal), TaskRunner (owns the
            QThread/worker lifecycle with run()/wait_all()), and
            install_gui_log_handler(bridge_parent, on_record) -> logging.Handler,
            which attaches a coloured GUI log handler to the root logger. The private
            _TaskWorker, _LogBridge and _GuiLogHandler back these public helpers; the
            log-level -> colour map lives with the handler.
Used by:    gui.app (MainController owns a TaskRunner and installs the log handler).
Depends on: PySide6 (QtCore threading/signals), model_atlas.config (get_settings),
            model_atlas.logging_setup (build_formatter).
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from model_atlas.config import get_settings
from model_atlas.logging_setup import build_formatter


class _LogBridge(QObject):
    record = Signal(str, str)


class ProgressBridge(QObject):
    # Emitted (from the worker thread) at the start of each pipeline phase; a queued connection
    # marshals it onto the GUI thread so the status label can be updated safely.
    phase = Signal(str)


class _GuiLogHandler(logging.Handler):
    def __init__(self, bridge: _LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - logging must not crash the GUI
            self.handleError(record)
            return
        colour = {
            logging.DEBUG: "#667085",
            logging.INFO: "#2563eb",
            logging.WARNING: "#b45309",
            logging.ERROR: "#b91c1c",
            logging.CRITICAL: "#7f1d1d",
        }.get(record.levelno, "#111827")
        self._bridge.record.emit(colour, message)


class _TaskWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self._fn())
        except Exception:  # noqa: BLE001 - surface all failures in the GUI
            self.failed.emit(traceback.format_exc())


class TaskRunner:
    """Own the QThread/worker lifecycle for background pipeline tasks.

    Keeps the live threads and workers referenced until each thread's ``finished``
    fires, so neither is garbage-collected mid-run; ``wait_all`` blocks on any still
    running (called from ``aboutToQuit`` so the app does not exit under a live worker).
    """

    def __init__(self, parent: QObject) -> None:
        self._parent = parent
        self._threads: list[QThread] = []
        self._workers: list[_TaskWorker] = []

    def run(
        self,
        fn: Callable[[], Any],
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ) -> None:
        thread = QThread(self._parent)
        worker = _TaskWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: self._threads.remove(thread) if thread in self._threads else None)
        thread.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self._workers.append(worker)
        thread.start()

    def wait_all(self) -> None:
        for thread in list(self._threads):
            if thread.isRunning():
                thread.wait()


def install_gui_log_handler(bridge_parent: QObject, on_record: Callable[[str, str], None]) -> logging.Handler:
    """Attach a coloured GUI log handler to the root logger and return it.

    ``on_record`` is invoked (as a queued slot, via the bridge parented to
    ``bridge_parent``) with ``(colour, message)`` for each emitted record. The handler
    is set to DEBUG so the GUI panel can show everything; per-logger level filtering is
    applied separately by the controller.
    """
    bridge = _LogBridge(bridge_parent)
    bridge.record.connect(on_record)
    handler = _GuiLogHandler(bridge)
    handler.setFormatter(build_formatter(get_settings().logging.format))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return handler
