"""Tests for file-centric preset matching (presets.matcher) + force-mode hard error."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import yaml

from model_atlas.pipeline import process
from model_atlas.presets.matcher import detect_file_format, match_file
from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.sources.container import FilesystemContainer, SourceFile


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("p.yaml"))


def _file(root: Path, rel: str) -> SourceFile:
    return SourceFile(containers=(FilesystemContainer(root),), logical_path=PurePosixPath(rel))


_CSV_A = """
preset: {id: a, name: A, version: 1.0, tier: secondary}
input_selector: {format: csv, name: loc.csv}
expected_columns: [lat, lon, ts]
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV_B = _CSV_A.replace("id: a, name: A", "id: b, name: B").replace(
    "expected_columns: [lat, lon, ts]", "expected_columns: [lat, lon, ts, extra1, extra2]"
)


def test_name_selector_matches_file(tmp_path: Path) -> None:
    (tmp_path / "loc.csv").write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    file = _file(tmp_path, "loc.csv")
    match = match_file(file, [_preset(_CSV_A)], root_prefix_depth=1)
    assert match is not None and match[0].name == "A"


def test_format_mismatch_skips_with_warning(tmp_path: Path, caplog) -> None:
    # A sqlite-declaring preset pointed at a CSV file: detected csv != declared sqlite.
    (tmp_path / "loc.csv").write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    sqlite_preset = _CSV_A.replace(
        "input_selector: {format: csv, name: loc.csv}",
        "input_selector: {format: sqlite, name: loc.csv, table: T}",
    )
    file = _file(tmp_path, "loc.csv")
    assert detect_file_format(file) == "csv"
    import logging
    with caplog.at_level(logging.WARNING):
        match = match_file(file, [_preset(sqlite_preset)], root_prefix_depth=1)
    assert match is None
    assert any("format mismatch" in r.message for r in caplog.records)


def test_structural_tie_break_prefers_better_fit(tmp_path: Path) -> None:
    # Both presets match by name+format; B declares more present columns -> higher score.
    (tmp_path / "loc.csv").write_text("lat,lon,ts,extra1,extra2\n1,2,3,4,5\n", encoding="utf-8")
    file = _file(tmp_path, "loc.csv")

    def peek(f, selector):
        return {"lat", "lon", "ts", "extra1", "extra2"}

    match = match_file(file, [_preset(_CSV_A), _preset(_CSV_B)], root_prefix_depth=1, peek=peek)
    assert match is not None and match[0].name == "B"


def test_force_mode_format_mismatch_is_hard_error(tmp_path: Path) -> None:
    # Force mode (single file + single preset) must hard-error on a format mismatch.
    src = tmp_path / "loc.csv"
    src.write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    preset = tmp_path / "p.yaml"
    preset.write_text(
        _CSV_A.replace(
            "input_selector: {format: csv, name: loc.csv}",
            "input_selector: {format: sqlite, name: loc.csv, table: T}",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="format mismatch"):
        process(src, preset, tmp_path / "out.csv", linked_entity="s")
