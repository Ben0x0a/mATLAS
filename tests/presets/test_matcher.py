"""Tests for file-centric preset matching (presets.matcher) + force-mode hard error."""
from __future__ import annotations

import sqlite3
from pathlib import Path, PurePosixPath

import pytest
import yaml

from model_atlas.app.pipeline import process
from model_atlas.ingest.containers import FilesystemContainer, SourceFile
from model_atlas.ingest.format_detect import detect_source_format
from model_atlas.presets.matcher import match_files_detailed, resolve_force_selector
from model_atlas.presets.spec import preset_spec_from_yaml


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("p.yaml"))


def _file(root: Path, rel: str) -> SourceFile:
    return SourceFile(containers=(FilesystemContainer(root),), logical_path=PurePosixPath(rel))


def _make_sqlite(path: Path, table: str) -> None:
    """Write a minimal on-disk SQLite database so the file carries the SQLite magic header
    (``detect_source_format`` classifies it ``sqlite`` regardless of its name)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE {table} (lat, lon, ts)")
        connection.commit()
    finally:
        connection.close()


def _match_one(file, presets, **kwargs):
    """The single best (preset, selector) for a file, or None — the matches list ([0] of the
    match_files_detailed tuple) reduced to its first element."""
    matches = match_files_detailed(file, presets, **kwargs)[0]
    return matches[0] if matches else None


_CSV_A = """
preset: {id: a, name: A, version: 1.0, tier: secondary}
input_selector: {format: csv, name: loc.csv}
expected_fields: [lat, lon, ts]
common: {entity: const(device)}
assertions:
  - position: {latitude: field(lat), longitude: field(lon)}
    time: {instant: field(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV_B = _CSV_A.replace("id: a, name: A", "id: b, name: B").replace(
    "expected_fields: [lat, lon, ts]", "expected_fields: [lat, lon, ts, extra1, extra2]"
)


def test_name_selector_matches_file(tmp_path: Path) -> None:
    (tmp_path / "loc.csv").write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    file = _file(tmp_path, "loc.csv")
    match = _match_one(file, [_preset(_CSV_A)], root_prefix_depth=1)
    assert match is not None and match[0].name == "A"


def test_format_mismatch_skips_with_warning(tmp_path: Path, caplog) -> None:
    # A sqlite-declaring preset pointed at a CSV file: detected csv != declared sqlite.
    (tmp_path / "loc.csv").write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    sqlite_preset = _CSV_A.replace(
        "input_selector: {format: csv, name: loc.csv}",
        "input_selector: {format: sqlite, name: loc.csv, table: T}",
    )
    file = _file(tmp_path, "loc.csv")
    assert detect_source_format(file) == "csv"
    import logging
    with caplog.at_level(logging.WARNING):
        match = _match_one(file, [_preset(sqlite_preset)], root_prefix_depth=1)
    assert match is None
    assert any("format mismatch" in r.message for r in caplog.records)


def test_structural_tie_break_prefers_better_fit(tmp_path: Path) -> None:
    # Both presets match by name+format; B declares more present columns -> higher score.
    (tmp_path / "loc.csv").write_text("lat,lon,ts,extra1,extra2\n1,2,3,4,5\n", encoding="utf-8")
    file = _file(tmp_path, "loc.csv")

    def peek(f, selector):
        return {"lat", "lon", "ts", "extra1", "extra2"}

    match = _match_one(file, [_preset(_CSV_A), _preset(_CSV_B)], root_prefix_depth=1, peek=peek)
    assert match is not None and match[0].name == "B"


# --- fix #1: required fields are gated through the glob-aware resolver ---------

_GLOB_REQUIRED = """
preset: {id: g, name: G, version: 1.0, tier: secondary}
input_selector: {format: csv, name: loc.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude: 'field("ZLAT*")', longitude: field(ZLONGITUDE)}
    time: {instant: field(ZTIMESTAMP), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_required_glob_field_satisfied_by_matching_column(tmp_path: Path) -> None:
    # A REQUIRED field mapped as a glob (``field("ZLAT*")``) is satisfied by a real column
    # (``ZLATITUDE``): the required-field gate must resolve the glob, not do a literal set
    # difference (which would report "ZLAT*" missing and disqualify the preset).
    (tmp_path / "loc.csv").write_text("ZLATITUDE,ZLONGITUDE,ZTIMESTAMP\n1,2,3\n", encoding="utf-8")
    file = _file(tmp_path, "loc.csv")

    def peek(f, selector):
        return {"ZLATITUDE", "ZLONGITUDE", "ZTIMESTAMP"}

    matches, unsatisfied = match_files_detailed(file, [_preset(_GLOB_REQUIRED)], root_prefix_depth=1, peek=peek)
    assert unsatisfied == []
    assert len(matches) == 1 and matches[0][0].name == "G"


# --- fix #2: no break — OR selectors and multi-sub-unit presets keep every match ---

_OR_CSV_SQLITE = """
preset: {id: o, name: O, version: 1.0, tier: secondary}
input_selector:
  - {format: csv, name: export.csv}
  - {format: sqlite, name: export.csv, table: T}
common: {entity: const(device)}
assertions:
  - position: {latitude: field(lat), longitude: field(lon)}
    time: {instant: field(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_or_selectors_match_both_a_csv_and_a_sqlite(tmp_path: Path) -> None:
    # One preset with two same-role OR selectors (a csv and a sqlite alternative sharing the
    # basename ``export.csv``). Both a real CSV and a real SQLite file must match — the SQLite
    # one only works because Step 1 keeps EVERY location-matching selector (no early break),
    # so the sqlite selector still reaches the format guard.
    def peek(f, selector):
        return {"lat", "lon", "ts"}

    preset = _preset(_OR_CSV_SQLITE)

    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "export.csv").write_text("lat,lon,ts\n1,2,3\n", encoding="utf-8")
    csv_file = _file(tmp_path, "csv/export.csv")
    assert detect_source_format(csv_file) == "csv"
    csv_matches = match_files_detailed(csv_file, [preset], root_prefix_depth=1, peek=peek)[0]
    assert len(csv_matches) == 1 and csv_matches[0][1].format == "csv"

    # A real SQLite database that happens to be named export.csv — magic wins over the suffix.
    _make_sqlite(tmp_path / "db" / "export.csv", "T")
    sqlite_file = _file(tmp_path, "db/export.csv")
    assert detect_source_format(sqlite_file) == "sqlite"
    sqlite_matches = match_files_detailed(sqlite_file, [preset], root_prefix_depth=1, peek=peek)[0]
    assert len(sqlite_matches) == 1 and sqlite_matches[0][1].format == "sqlite"


_TWO_TABLES = """
preset: {id: m, name: M, version: 1.0, tier: secondary}
input_selector:
  - {format: sqlite, name: multi.sqlite, table: ZRTCLLOCATION}
  - {format: sqlite, name: multi.sqlite, table: ZRTLEARNEDLOCATION}
common: {entity: const(device)}
assertions:
  - position: {latitude: field(lat), longitude: field(lon)}
    time: {instant: field(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_two_table_selectors_extract_from_both_tables(tmp_path: Path) -> None:
    # A preset declaring two sqlite selectors for DIFFERENT tables of one DB must match BOTH
    # (distinct sub-units, so both survive grouping) — the early break used to keep only the
    # first, silently never extracting the second table's rows.
    _make_sqlite(tmp_path / "multi.sqlite", "ZRTCLLOCATION")
    file = _file(tmp_path, "multi.sqlite")
    assert detect_source_format(file) == "sqlite"

    def peek(f, selector):
        return {"lat", "lon", "ts"}

    matches = match_files_detailed(file, [_preset(_TWO_TABLES)], root_prefix_depth=1, peek=peek)[0]
    assert {selector.table for _preset_spec, selector in matches} == {
        "ZRTCLLOCATION", "ZRTLEARNEDLOCATION"}


def test_force_mode_resolves_an_absent_subunit_for_every_format(tmp_path: Path) -> None:
    """The structural fallback must retarget the attribute that actually holds the unit.

    It used to write ``table=`` whatever the format, so an xml preset whose declared model
    was absent got a selector with the WRONG model and a stray table — the fallback silently
    did nothing. Both formats now go through ``InputSelector.retargeted``."""
    xml_preset = _preset(_CSV_A.replace(
        "input_selector: {format: csv, name: loc.csv}",
        "input_selector: {format: xml, name: report.xml, model: Absent}",
    ))
    excel_preset = _preset(_CSV_A.replace(
        "input_selector: {format: csv, name: loc.csv}",
        "input_selector: {format: excel, name: book.xlsx, sheet: Absent}",
    ))
    file = _file(tmp_path, "report.xml")

    def peek(_file, _selector):
        return {"lat", "lon", "ts"}

    resolved = resolve_force_selector(
        file, xml_preset, "xml", subtables=lambda f, s: ["Location"], peek=peek)
    assert resolved.model == "Location" and resolved.table is None

    resolved = resolve_force_selector(
        _file(tmp_path, "book.xlsx"), excel_preset, "excel",
        subtables=lambda f, s: ["Sheet1"], peek=peek)
    assert resolved.sheet == "Sheet1"


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
        process(src, preset, tmp_path / "out", concerns="s")
