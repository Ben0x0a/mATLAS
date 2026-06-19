"""PySide6 launcher for the Model Atlas GUI."""
from __future__ import annotations

import sys
import os
import signal


def run(argv: list[str] | None = None) -> int:
    """Start the GUI event loop."""
    try:
        from importlib.resources import files

        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print(
            "PySide6 is not installed. Install the GUI extra with: "
            "python -m pip install '.[gui]'",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    from gui.app import MainController
    from gui.theme import configure_application_font

    app = QApplication(sys.argv[:1] + (argv or []))
    app.setStyle("Fusion")
    configure_application_font(app)
    app.setApplicationName("Model ATLAS Transformer")
    app.setWindowIcon(QIcon(str(files("gui").joinpath(
        "assets",
        "matlas_transformer_desktop_icon.svg",
    ))))
    _install_sigint_handler(app)
    controller = MainController()
    controller.window.show()
    return int(app.exec())


def _install_sigint_handler(app: object) -> None:
    """Make terminal Ctrl+C terminate the GUI process immediately.

    Qt's C++ event loop never yields to the Python interpreter, so a SIGINT
    handler installed in Python alone never runs while ``app.exec()`` blocks. A
    periodic no-op QTimer wakes the interpreter often enough to let pending
    signals be delivered.
    """
    from PySide6.QtCore import QTimer

    def _handle_sigint(signum: int, _frame: object) -> None:
        try:
            sys.stderr.write("\nInterrupted by Ctrl+C; exiting.\n")
            sys.stderr.flush()
        finally:
            os._exit(128 + signum)

    signal.signal(signal.SIGINT, _handle_sigint)

    timer = QTimer()
    timer.start(200)
    timer.timeout.connect(lambda: None)
    # Keep a reference on the app so the timer is not garbage-collected.
    app._sigint_timer = timer


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
