"""The shared behaviour of the diagnostics button that both action bars carry.

Defines:    ``REPORT_BUG_TEXT`` / ``OPEN_ERRORS_TEXT`` (the two captions), ``sync_report_button``
            (set the caption, tooltip and accessible description from what is on disk) and
            ``handle_report_button`` (open the folder, or file a bug report and reveal it).
Used by:    gui.app (the Transform action bar) and gui.spatiotemporal_controller (the
            concomitance action bar) — one behaviour, so the two bars cannot drift.
Depends on: model_atlas.app.diagnostics (report_dir / error_report_count / write_bug_report)
            and PySide6 for the message box + folder reveal only.

WHY one button with two captions rather than two buttons: they answer the same operator
question — "something is wrong, how do I hand it over?" — and only one of them is ever the
right answer. When the tool has already failed, the evidence exists and the job is to find
it; when it has not, there is nothing to open and the job is to capture the state that a
silent bug leaves nowhere else.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QPushButton, QWidget

from model_atlas.app.diagnostics import error_report_count, report_dir, write_bug_report

log = logging.getLogger("model_atlas.gui")

OPEN_ERRORS_TEXT = "Open error reports"
REPORT_BUG_TEXT = "Report bug…"


def sync_report_button(button: QPushButton) -> None:
    """Point the button at whichever job applies right now.

    The caption is driven by whether any ERROR report exists on disk — bug reports are
    counted separately on purpose, so filing one cannot make the tool claim it has errored.
    Call this at start-up and after anything that might have produced a report (the end of a
    run), so the caption never lags behind the folder.
    """
    count = error_report_count()
    if count:
        button.setText(OPEN_ERRORS_TEXT)
        button.setToolTip(
            f"{count} error report(s) written to {report_dir()}. Open the folder.")
        button.setAccessibleName("Open error reports")
        button.setAccessibleDescription(
            f"Open the folder holding {count} error report(s).")
    else:
        button.setText(REPORT_BUG_TEXT)
        button.setToolTip(
            "Nothing has failed. If the tool did the wrong thing WITHOUT an error, file a "
            "bug report: it captures every thread's variables right now, which a silent "
            "bug leaves nowhere else.")
        button.setAccessibleName("Report a bug")
        button.setAccessibleDescription(
            "Capture the current state of every thread to a report file, for a problem that "
            "produced no error.")


def handle_report_button(parent: QWidget, button: QPushButton, *,
                         context: dict[str, Any] | None = None) -> None:
    """Run whichever job the button is currently offering, then re-sync its caption."""
    if error_report_count():
        _open_folder(parent)
    else:
        _file_bug_report(parent, context)
    sync_report_button(button)


def _open_folder(parent: QWidget) -> None:
    directory = report_dir()
    if not directory.is_dir():
        # Only reachable if the folder vanished between the count and the click.
        QMessageBox.information(
            parent, "Model Atlas", f"No reports yet.\nThey would be written to:\n{directory}")
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))


def _file_bug_report(parent: QWidget, context: dict[str, Any] | None) -> None:
    """Capture the live state, then tell the operator what to do with it.

    The capture happens on the GUI thread deliberately: it must observe the other threads
    mid-work, and moving it off-thread would only add one more stack to describe."""
    path = write_bug_report(context=context)
    if path is None:
        QMessageBox.warning(
            parent, "Model Atlas",
            "Could not write the bug report. See the log for why.\n"
            "Diagnostics may be disabled in the configuration.")
        return
    log.info(f"Bug report written to {path}")
    answer = QMessageBox.information(
        parent, "Model Atlas",
        f"Bug report written to:\n{path.name}\n\n"
        "It holds every thread's variables as they are right now, plus empty "
        "'Steps to reproduce' and 'Expected vs actual' sections in the .md twin — "
        "please fill those in.\n\n"
        "The report contains run-time values: review the 'Sensitive' section before "
        "sharing it.\n\nOpen the folder now?",
        QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Close,
        QMessageBox.StandardButton.Open,
    )
    if answer == QMessageBox.StandardButton.Open:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
