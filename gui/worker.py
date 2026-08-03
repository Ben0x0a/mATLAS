"""Background-thread plumbing and the GUI log handler for the PySide6 GUI.

Defines:    ProgressBridge (thread-safe per-phase signal), TaskRunner (a QObject owning the
            QThread/worker lifecycle with run()/wait_all(), whose bookkeeping runs on the GUI
            thread), install_gui_log_handler(bridge_parent, on_record) -> logging.Handler
            (attaches a coloured GUI log handler to the root logger) and
            remove_gui_log_handler(handler) (detaches it on teardown). The private
            _TaskWorker, _LogBridge and _GuiLogHandler back these public helpers; the
            log-level -> colour map lives with the handler.
Used by:    gui.app (MainController owns a TaskRunner, installs and — on close — removes the
            log handler) and gui.spatiotemporal_controller (reuses the shared TaskRunner).
Depends on: PySide6 (QtCore threading/signals), model_atlas.app.config (get_settings),
            model_atlas.app.logging_setup (build_formatter).
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from model_atlas.app.config import get_settings
from model_atlas.app.logging_setup import build_formatter


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


class TaskRunner(QObject):
    """Own the QThread/worker lifecycle for background pipeline tasks.

    Keeps the live threads and workers referenced until each thread's ``finished``
    fires, so neither is garbage-collected mid-run; ``wait_all`` blocks on any still
    running (called from ``aboutToQuit`` so the app does not exit under a live worker).

    A ``QObject`` (parented to the caller) so the bookkeeping runs on the GUI thread: the
    ``_reap`` slot below is a bound method of this GUI-thread object, so Qt marshals each
    ``thread.finished`` (emitted from the FINISHING worker thread) onto the GUI thread via a
    queued connection. That keeps every mutation of ``_threads``/``_workers`` on one thread —
    the same thread ``run`` and ``wait_all`` touch them from — so rapid successive runs cannot
    corrupt the lists by racing an append against a cross-thread remove.
    """

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        # Index-aligned: run() appends to both together, so _workers[i] is _threads[i]'s worker.
        self._threads: list[QThread] = []
        self._workers: list[_TaskWorker] = []

    def run(
        self,
        fn: Callable[[], Any],
        on_finished: Callable[[Any], None],
        on_failed: Callable[[str], None],
    ) -> None:
        thread = QThread(self)
        worker = _TaskWorker(fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        worker.failed.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        # _reap is a bound slot of this GUI-thread QObject, so this connection is queued onto
        # the GUI thread — the list bookkeeping never runs on the finishing worker thread.
        thread.finished.connect(self._reap)
        thread.finished.connect(thread.deleteLater)
        self._threads.append(thread)
        self._workers.append(worker)
        thread.start()

    @Slot()
    def _reap(self) -> None:
        """Drop every finished thread + its paired worker (GUI thread, via a queued connection).

        Prunes by ``isFinished`` rather than the specific sender so the index-aligned lists stay
        paired; running the removal on the GUI thread is what makes it race-free.
        """
        kept_threads: list[QThread] = []
        kept_workers: list[_TaskWorker] = []
        for thread, worker in zip(self._threads, self._workers):
            if thread.isFinished():
                continue
            kept_threads.append(thread)
            kept_workers.append(worker)
        self._threads = kept_threads
        self._workers = kept_workers

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


def remove_gui_log_handler(handler: logging.Handler | None) -> None:
    """Detach a handler installed by :func:`install_gui_log_handler` from the root logger.

    WHY this must be called on window close/teardown: the handler forwards records to the log
    widget for the WHOLE process, so a record emitted after the window (and its ``log_edit``)
    is torn down would call ``append`` on a deleted Qt widget and crash. Both the running app
    and the GUI tests reach this through ``MainController.shutdown``, which detaches the
    handler LAST so records emitted while the workers drain still reach the log panel.
    """
    if handler is not None:
        logging.getLogger().removeHandler(handler)
