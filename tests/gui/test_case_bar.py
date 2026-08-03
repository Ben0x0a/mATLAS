"""Tests for the Transform tab's case bar (gui.app: New case / Open case + summary).

Builds the real MainController offscreen, creates a case via the pipeline, then drives the
case-bar handlers directly (QFileDialog / QInputDialog monkeypatched) to assert: Open case
binds the case (title, summary, declared identity, both analysis tabs), New case declares an
identity from a name + folder, Close case detaches it and restores the two buttons, and a
file that is not a mATLAS case DB is refused without a crash.

The case name and output folder are no longer form fields — they live on the controller as
_case_name / _case_output_dir, set by the case bar — so the assertions read that state.
Used by:    pytest (the shared ``controller`` fixture and the headless offscreen platform both
            come from tests/gui/conftest.py).
Depends on: gui.app (MainController), model_atlas.app.pipeline, the golden preset fixture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QFileDialog, QInputDialog

from gui.app import WINDOW_TITLE_BASE
from model_atlas.app.pipeline import process

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
_HEADER = ("Latitude,Longitude,Accuracy (m),Speed (m/s),"
           "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy),Source,Location,Item ID\n")


def _build_case(tmp_path: Path) -> Path:
    source = tmp_path / "a.csv"
    source.write_text(_HEADER + "46.5,6.6,12.0,3.0,06.12.2025 13:00:00.000,"
                      "/x/C.sqlite,Table: T(Z_PK: 1),1001\n", encoding="utf-8")
    out = tmp_path / "case"
    process(source, _GOLDEN / "secondary_cached.preset.yaml", out,
            concerns="subject", base_name="op")
    return out / "op.matlas.sqlite"


def test_open_case_binds_context_then_close_case_detaches(controller, tmp_path, monkeypatch) -> None:
    db = _build_case(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(db), "")))

    controller._open_case()
    assert controller._active_case_db == db
    assert "Case 'op'" in controller.case_summary_label.text()
    # The case bar now owns the identity the run writes to.
    assert controller._case_name == "op"
    assert controller._case_output_dir == db.parent
    assert controller._case_db_path() == db
    assert controller.window.windowTitle().endswith("Case: op")
    # Both analysis tabs bind to the opened case.
    assert controller._db_edit_controller.path_edit.text() == str(db)
    assert controller._spatiotemporal_controller.db_edit.text() == str(db)
    # With a case open, the bar offers only Close case.
    assert controller.close_case_button.isVisibleTo(controller.window)
    assert not controller.new_case_button.isVisibleTo(controller.window)

    controller._close_case()
    assert controller._active_case_db is None
    assert controller._case_name is None
    assert controller._case_output_dir is None
    assert controller.case_summary_label.text() == "No case open"
    assert controller.window.windowTitle() == WINDOW_TITLE_BASE
    assert controller.new_case_button.isVisibleTo(controller.window)
    assert not controller.close_case_button.isVisibleTo(controller.window)


def test_new_case_declares_name_and_folder(controller, tmp_path, monkeypatch) -> None:
    controller._close_case()
    folder = tmp_path / "fresh"
    folder.mkdir()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("op2", True)))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(folder)))

    controller._new_case()
    assert controller._case_name == "op2"
    assert controller._case_output_dir == folder
    # Declared only: no run has written the database yet, so no case is *active*.
    assert controller._active_case_db is None
    assert controller._case_db_path() == folder / "op2.matlas.sqlite"
    assert "op2" in controller.case_summary_label.text()
    controller._close_case()


def test_new_case_adopts_an_existing_case_in_that_folder(controller, tmp_path, monkeypatch) -> None:
    # Naming a case that already exists there must adopt it, because a run appends to an
    # existing case database rather than starting over.
    adopt_root = tmp_path / "adopt"
    adopt_root.mkdir()
    db = _build_case(adopt_root)
    controller._close_case()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("op", True)))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(db.parent)))

    controller._new_case()
    assert controller._active_case_db == db
    assert controller._db_edit_controller.path_edit.text() == str(db)
    controller._close_case()


def test_new_case_rejects_a_name_that_would_escape_the_folder(controller, tmp_path, monkeypatch) -> None:
    controller._close_case()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("../evil", True)))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(tmp_path)))
    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", errors.append)

    controller._new_case()
    assert errors, "a path separator in the case name must be refused"
    assert controller._case_name is None


def test_cancelling_either_new_case_dialog_changes_nothing(controller, tmp_path, monkeypatch) -> None:
    controller._close_case()
    # Cancel at the name prompt.
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    controller._new_case()
    assert controller._case_name is None
    # Accept the name, then cancel the folder picker: still no half-declared case.
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("op3", True)))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: ""))
    controller._new_case()
    assert controller._case_name is None


def test_open_case_refuses_a_non_case_file(controller, tmp_path, monkeypatch) -> None:
    controller._close_case()   # start from a detached state, independent of test order
    stray = tmp_path / "not_a_case.sqlite"
    stray.write_text("nonsense", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(stray), "")))
    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", errors.append)

    controller._open_case()
    assert errors and "not a matlas case database" in errors[0].lower()
    assert controller._active_case_db is None
