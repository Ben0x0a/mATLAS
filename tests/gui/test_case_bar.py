"""Tests for the Transform tab's case bar (gui.app: New case / Open case + summary).

Builds the real MainController offscreen, creates a case via the pipeline, then drives the
case-bar handlers directly (QFileDialog monkeypatched) to assert: Open case binds the case
(title, summary, output/name fields, both analysis tabs), New case detaches it, and a file
that is not a mATLAS case DB is refused without a crash.
Used by:    pytest (headless: QT_QPA_PLATFORM=offscreen forced below).
Depends on: gui.app (MainController), model_atlas.app.pipeline, the golden preset fixture.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QApplication, QFileDialog

from gui.app import MainController, WINDOW_TITLE_BASE
from model_atlas.app.pipeline import process

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
_HEADER = ("Latitude,Longitude,Accuracy (m),Speed (m/s),"
           "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy),Source,Location,Item ID\n")


@pytest.fixture(scope="module")
def controller():
    app = QApplication.instance() or QApplication([])
    ctrl = MainController()
    app.processEvents()
    yield ctrl
    ctrl._runner.wait_all()
    app.processEvents()
    if ctrl._log_handler is not None:
        logging.getLogger().removeHandler(ctrl._log_handler)
    ctrl.window.deleteLater()
    app.processEvents()


def _build_case(tmp_path: Path) -> Path:
    source = tmp_path / "a.csv"
    source.write_text(_HEADER + "46.5,6.6,12.0,3.0,06.12.2025 13:00:00.000,"
                      "/x/C.sqlite,Table: T(Z_PK: 1),1001\n", encoding="utf-8")
    out = tmp_path / "case"
    process(source, _GOLDEN / "secondary_cached.preset.yaml", out,
            linked_entity="subject", base_name="op")
    return out / "op.matlas.sqlite"


def test_open_case_binds_context_then_new_case_detaches(controller, tmp_path, monkeypatch) -> None:
    db = _build_case(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(db), "")))

    controller._open_case()
    assert controller._active_case_db == db
    assert "Case 'op'" in controller.case_summary_label.text()
    assert controller.case_name_edit.text() == "op"
    assert controller.output_edit.text() == str(db.parent)
    assert controller.window.windowTitle().endswith("Case: op")
    # Both analysis tabs bind to the opened case.
    assert controller._db_edit_controller.path_edit.text() == str(db)
    assert controller._spatiotemporal_controller.db_edit.text() == str(db)

    controller._new_case()
    assert controller._active_case_db is None
    assert controller.case_name_edit.text() == ""
    assert controller.case_summary_label.text() == "No case open"
    assert controller.window.windowTitle() == WINDOW_TITLE_BASE


def test_open_case_refuses_a_non_case_file(controller, tmp_path, monkeypatch) -> None:
    controller._new_case()   # start from a detached state, independent of test order
    stray = tmp_path / "not_a_case.sqlite"
    stray.write_text("nonsense", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(stray), "")))
    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", errors.append)

    controller._open_case()
    assert errors and "not a matlas case database" in errors[0].lower()
    assert controller._active_case_db is None
