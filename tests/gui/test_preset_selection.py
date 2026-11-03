"""Tests for moving presets between the available and selected lists (gui.app).

Covers the search-aware "add all": with a search term typed, the button must add exactly
the presets the analyst can SEE, never the ones the filter hid. Also pins the plain
selection path and the duplicate guard.
Used by:    pytest (the shared ``controller`` fixture and the offscreen platform both come
            from tests/gui/conftest.py).
Depends on: gui.app (MainController), PySide6.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import Qt

from tests.gui.conftest import drain


def _reset(controller) -> None:
    controller.selected_preset_list.clear()
    controller.preset_search_edit.clear()
    controller._render_available_presets()


def _names(list_widget) -> list[str]:
    return [list_widget.item(row).text() for row in range(list_widget.count())]


def _a_search_needle(controller) -> str:
    """A term matching some but not all loaded presets, taken from a real preset name."""
    for preset in controller._presets:
        needle = preset.name.strip().split(" ")[0]
        if not needle:
            continue
        matching = [p for p in controller._presets if needle.casefold() in p.name.casefold()]
        if 0 < len(matching) < len(controller._presets):
            return needle
    pytest.skip("no loaded preset name discriminates a strict subset")


def test_add_all_adds_only_the_filtered_presets(controller) -> None:
    _reset(controller)
    assert len(controller._presets) > 1
    needle = _a_search_needle(controller)

    controller.preset_search_edit.setText(needle)
    visible = _names(controller.available_preset_list)
    assert 0 < len(visible) < len(controller._presets)

    controller._add_all_presets()

    assert _names(controller.selected_preset_list) == visible
    # The hidden ones stayed out — and reappear once the search is cleared.
    controller.preset_search_edit.clear()
    remaining = _names(controller.available_preset_list)
    assert remaining
    assert not set(remaining) & set(visible)
    _reset(controller)


def test_add_all_without_a_search_adds_every_preset(controller) -> None:
    _reset(controller)
    controller._add_all_presets()
    assert controller.selected_preset_list.count() == len(controller._presets)
    assert controller.available_preset_list.count() == 0
    _reset(controller)


def test_add_all_skips_presets_already_selected(controller) -> None:
    _reset(controller)
    first = controller.available_preset_list.item(0)
    first.setSelected(True)
    controller._add_selected_presets()
    assert controller.selected_preset_list.count() == 1

    controller._add_all_presets()

    paths = [
        controller.selected_preset_list.item(row).data(Qt.ItemDataRole.UserRole)
        for row in range(controller.selected_preset_list.count())
    ]
    assert len(paths) == len(set(paths)) == len(controller._presets)
    _reset(controller)


def test_missing_presets_folder_tells_how_to_fetch_the_submodule(
        controller, app, tmp_path, monkeypatch) -> None:
    # An install step, not a crash: a background failure would write a crash report.
    reports = []
    monkeypatch.setattr("gui.worker.capture_exception", lambda *a, **k: reports.append(a))
    original = controller.presets_path_edit.text()
    try:
        controller.presets_path_edit.setText(str(tmp_path / "absent"))
        controller._load_presets_async()
        controller._runner.wait_all()
        drain(app)
        assert "git submodule update --init" in controller.preset_status_label.text()
        assert str(tmp_path / "absent") in controller.preset_status_label.toolTip()
        assert reports == []
    finally:
        controller.presets_path_edit.setText(original)
        controller._load_presets_async()
        controller._runner.wait_all()
        drain(app)
    assert controller.preset_status_label.toolTip() == ""


@pytest.mark.parametrize("auto_mode", [True, False])
def test_run_is_refused_with_the_fetch_command_when_presets_are_missing(
        controller, tmp_path, monkeypatch, auto_mode: bool) -> None:
    errors: list[str] = []
    monkeypatch.setattr(controller, "_show_error", errors.append)
    monkeypatch.setattr(controller, "_start_run", lambda: pytest.fail("the run must not start"))
    monkeypatch.setattr(controller, "_case_output_dir", tmp_path)
    monkeypatch.setattr(controller, "_case_name", "case")
    original = controller.presets_path_edit.text()
    original_mode = controller.auto_preset_check.isChecked()
    controller.auto_preset_check.setChecked(auto_mode)
    controller.selected_preset_list.clear()
    controller.source_edit.setText(str(tmp_path))
    controller.concerns_edit.setText("Subject")
    controller.presets_path_edit.setText(str(tmp_path / "absent"))
    try:
        controller._run_process()
    finally:
        controller.presets_path_edit.setText(original)
        controller.auto_preset_check.setChecked(original_mode)
        controller.source_edit.clear()
        controller.concerns_edit.clear()
    assert len(errors) == 1 and "git submodule update --init" in errors[0]
