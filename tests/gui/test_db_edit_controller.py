"""Headless smoke tests for the Database Edit panel (gui.db_edit_controller).

Defines:    a construction test (the controller builds against a bare offscreen QWidget), an
            end-to-end edit test (open a real persisted DB, edit an integration coordinate,
            save, re-open and see the change + the edit_log entry), an evidence-protection UI
            test (Save-edits disabled with a tooltip on an evidence row), and unit tests of
            the pure mapping helpers (``_coerce`` / ``_row_summary``).
Used by:    pytest (headless: QT_QPA_PLATFORM=offscreen is forced below).
Depends on: gui.db_edit_controller, model_atlas.app.edit_api, PySide6, and the persisted-DB
            builder reused from tests.app.test_edit_api.
"""
from __future__ import annotations

import os
from pathlib import Path

# Force offscreen BEFORE any Qt import so the suite runs headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QWidget

from gui.db_edit_controller import DbEditController, _coerce, _row_summary
from model_atlas.app.edit_api import EditApi
from tests.app.test_edit_api import _build_persisted_db
from tests.gui.conftest import drain


@pytest.fixture
def persisted_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    target = tmp_path / "case.matlas.sqlite"
    return target, _build_persisted_db(target)


# --- pure helpers ----------------------------------------------------------

def test_coerce_matches_original_type() -> None:
    assert _coerce("46.2", 46.193) == 46.2          # REAL stays float
    assert _coerce("7", 3) == 7                      # INTEGER stays int
    assert _coerce("", "anything") is None           # empty -> NULL
    assert _coerce("text", None) == "text"           # unknown type -> raw string


def test_row_summary_leads_with_id() -> None:
    summary = _row_summary({"entity_id": "abc", "identifier": "+41", "type": "device"})
    assert summary.startswith("abc")
    assert "identifier=+41" in summary


# --- construction ----------------------------------------------------------

def test_constructs_against_bare_parent(app) -> None:
    parent = QWidget()
    controller = DbEditController(parent)
    # No DB open yet: the action buttons are disabled.
    assert not controller.save_button.isEnabled()
    assert not controller.new_row_button.isEnabled()
    parent.deleteLater()
    drain(app)   # run that delete NOW rather than at some later timer tick


# --- end-to-end edit through the panel ------------------------------------

def _select_leaf(controller: DbEditController, table: str):
    """Select the first row of ``table`` in the navigator and return the tree item."""
    nav = controller.navigator
    for i in range(nav.topLevelItemCount()):
        top = nav.topLevelItem(i)
        if top.text(0).startswith(table + " ") and top.childCount():
            leaf = top.child(0)
            nav.setCurrentItem(leaf)
            return leaf
    raise AssertionError(f"no populated {table!r} node in the navigator")


def test_edit_integration_saves_and_persists(app, persisted_db) -> None:
    path, keys = persisted_db
    si_id = keys["spatial_integration_id"]
    parent = QWidget()
    controller = DbEditController(parent)
    controller.open_database(path)

    _select_leaf(controller, "spatial_integration")
    assert controller.save_button.isEnabled()          # integration row is editable
    controller._field_edits["latitude_wgs84"].setText("46.5")
    controller._on_save_edits()

    # Re-open a fresh API: the coordinate change and its audit entry are on disk.
    with EditApi.open(path) as api:
        assert api.row("spatial_integration", si_id)["latitude_wgs84"] == 46.5
        log = api.edit_log()
        assert len(log) == 1
        assert log[0]["column"] == "latitude_wgs84"
        assert log[0]["new_value"] == "46.5"
    parent.deleteLater()
    drain(app)   # run that delete NOW rather than at some later timer tick


def test_evidence_row_disables_save(app, persisted_db) -> None:
    path, _keys = persisted_db
    parent = QWidget()
    controller = DbEditController(parent)
    controller.open_database(path)

    _select_leaf(controller, "entity")
    assert not controller.save_button.isEnabled()      # evidence: no in-place edit
    assert "Save as new row" in controller.save_button.toolTip()
    # But minting a new occurrence is offered.
    assert controller.new_row_button.isEnabled()
    parent.deleteLater()
    drain(app)   # run that delete NOW rather than at some later timer tick
