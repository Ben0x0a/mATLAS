"""Tests for the single-preset notice decision helper (gui.app).

The "one preset applied without filename selector filtering" notice must be shown only
when the pipeline's force-preset mode will actually fire: a single preset in play AND a
single non-archive file source. This exercises the pure decision function directly, with
no QApplication.
Used by:    pytest.
Depends on: gui.app._should_show_single_preset_notice (imports PySide6 but needs no Qt app).
"""
from __future__ import annotations

from pathlib import Path

from gui.app import _should_show_single_preset_notice


def _make_file(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text("x", encoding="utf-8")
    return str(path)


def test_single_selected_preset_with_plain_file_shows_notice(tmp_path: Path) -> None:
    source = _make_file(tmp_path, "in.csv")
    assert _should_show_single_preset_notice(True, [Path("p.yaml")], 0, source) is True


def test_single_loaded_preset_in_auto_mode_with_file_shows_notice(tmp_path: Path) -> None:
    source = _make_file(tmp_path, "in.csv")
    # Auto mode: "single preset in play" is driven by the loaded-preset count, not selection.
    assert _should_show_single_preset_notice(False, [], 1, source) is True


def test_zip_source_hides_notice(tmp_path: Path) -> None:
    source = _make_file(tmp_path, "acq.zip")
    assert _should_show_single_preset_notice(True, [Path("p.yaml")], 0, source) is False


def test_folder_source_hides_notice(tmp_path: Path) -> None:
    assert _should_show_single_preset_notice(True, [Path("p.yaml")], 0, str(tmp_path)) is False


def test_missing_or_empty_source_hides_notice(tmp_path: Path) -> None:
    assert _should_show_single_preset_notice(True, [Path("p.yaml")], 0, "") is False
    assert _should_show_single_preset_notice(True, [Path("p.yaml")], 0, str(tmp_path / "nope.csv")) is False


def test_multiple_presets_hide_notice(tmp_path: Path) -> None:
    source = _make_file(tmp_path, "in.csv")
    # Two selected presets, or two loaded presets in auto mode: not a single preset in play.
    assert _should_show_single_preset_notice(True, [Path("a.yaml"), Path("b.yaml")], 0, source) is False
    assert _should_show_single_preset_notice(False, [], 2, source) is False
