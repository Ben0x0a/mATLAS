"""Tests for the feature batch: temp staging, force-preset, from_file mapping,
entity/linked_entity run-level defaults, and a synthetic SQLite round-trip (regression
guard for the Windows connection-close fix).

Used by:    pytest.
Depends on: pipeline, sources.discover, sqlite reader, pandas.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from model_atlas.pipeline import process

# A CSV preset whose selector intentionally points at a DIFFERENT file name, so it
# only runs under force-preset mode.
_PRESET_OTHER_NAME = """
preset: {id: t.t, name: T, version: 1.0, tier: secondary}
input_selector: {format: csv, name: "does-not-match.csv"}
source_record_uid: column(Item)
common:
  input_record_id: filename(name)
  source_label: filename(stem)
  raw_source_path: filename(path)
assertions:
  - position: {latitude_wgs84: column(Lat), longitude_wgs84: column(Lon)}
    time: {instant: column(TS), format: "%d.%m.%Y %H:%M:%S.%f"}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV = "Lat,Lon,TS,Item\n1.5,2.5,06.12.2025 13:00:00.000,A\n"


def _write_csv(tmp_path: Path) -> Path:
    source = tmp_path / "data.csv"
    source.write_text(_CSV, encoding="utf-8")
    return source


def _read_rows(csv_path: Path) -> list[dict[str, str]]:
    import csv

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_force_preset_applies_despite_selector_mismatch(tmp_path: Path) -> None:
    """One input file + one preset YAML => preset applied even though its selector
    file_name does not match the source name."""
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_OTHER_NAME, encoding="utf-8")
    output = tmp_path / "out.csv"

    result = process(source, preset, output, linked_entity="subject")

    assert result.row_counts["rows"] == 1
    assert result.matched and "-> T" in result.matched[0]


def test_from_file_tokens_populate_fields(tmp_path: Path) -> None:
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_OTHER_NAME, encoding="utf-8")
    output = tmp_path / "out.csv"

    process(source, preset, output, linked_entity="subject")
    row = _read_rows(output)[0]

    assert row["input_record_id"] == "data.csv"      # from_file: name
    assert row["source_label"] == "data"          # from_file: stem
    assert row["raw_source_path"].endswith("data.csv")  # from_file: path


def test_entity_and_linked_entity_defaults_fill_when_preset_omits(tmp_path: Path) -> None:
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_OTHER_NAME, encoding="utf-8")  # sets neither entity nor linked_entity
    output = tmp_path / "out.csv"

    process(source, preset, output, entity="device-7", linked_entity="Daphne")
    row = _read_rows(output)[0]

    assert row["entity"] == "device-7"
    assert row["linked_entity"] == "Daphne"


_PRESET_WITH_ENTITY = _PRESET_OTHER_NAME.replace(
    "common:\n", "common:\n  entity: const(preset-entity)\n"
)


def test_run_level_entity_overrides_preset(tmp_path: Path) -> None:
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_WITH_ENTITY, encoding="utf-8")
    output = tmp_path / "out.csv"

    # The arg is authoritative; the preset's entity is only the default.
    process(source, preset, output, entity="run-level", linked_entity="Daphne")
    row = _read_rows(output)[0]

    assert row["entity"] == "run-level"


def test_preset_entity_used_when_no_arg(tmp_path: Path) -> None:
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_WITH_ENTITY, encoding="utf-8")
    output = tmp_path / "out.csv"

    # No --entity: the preset default stands.
    process(source, preset, output, linked_entity="Daphne")
    row = _read_rows(output)[0]

    assert row["entity"] == "preset-entity"


_SQLITE_PRESET = """
preset: {id: t.s, name: S, version: 1.0, tier: secondary}
input_selector: {format: sqlite, name: loc.sqlite, table: LOCATIONS}
source_record_uid: column(id)
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_process_requires_linked_entity_argument(tmp_path: Path) -> None:
    """The package API declares linked_entity as a required keyword: omitting it is a
    TypeError, not a silent None."""
    source = _write_csv(tmp_path)
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_OTHER_NAME, encoding="utf-8")
    with pytest.raises(TypeError):
        process(source, preset, tmp_path / "out.csv")  # type: ignore[call-arg]


def test_cli_requires_linked_entity() -> None:
    from launcher.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["process", "--input", "x", "--output", "y"])
    # With --linked-entity it parses fine.
    args = parser.parse_args(["process", "--input", "x", "--output", "y", "--linked-entity", "s"])
    assert args.linked_entity == "s"
    assert args.entity is None


def test_discovery_never_creates_sidecars_next_to_original(tmp_path: Path) -> None:
    """A WAL-mode database must not gain -wal/-shm/-journal siblings when mATLAS reads
    it: the original evidence is opened immutably, never mutated. Regression guard for
    the discovery probe (mode=ro alone leaks sidecars; immutable=1 does not)."""
    from model_atlas.sources.discover import discover

    db = tmp_path / "loc.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # persists in the file header
        conn.execute("CREATE TABLE LOCATIONS (id INTEGER, lat REAL, lon REAL, ts REAL)")
        conn.execute("INSERT INTO LOCATIONS VALUES (1, 1.5, 2.5, 1700000000.0)")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    # Clear any sidecars our own setup connection may have left.
    for suffix in ("-wal", "-shm", "-journal"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)

    files = discover(db)

    assert len(files) == 1
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db.with_name(db.name + suffix)
        assert not sidecar.exists(), f"discovery created {sidecar.name} next to the original"


def test_sqlite_roundtrip_releases_file(tmp_path: Path) -> None:
    """Synthetic SQLite source extracts cleanly (regression guard for the
    connection-close fix: a leaked handle would block temp cleanup on Windows)."""
    db = tmp_path / "loc.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE LOCATIONS (id INTEGER, lat REAL, lon REAL, ts REAL)")
        conn.execute("INSERT INTO LOCATIONS VALUES (1, 1.5, 2.5, 1700000000.0)")
        conn.commit()
    finally:
        conn.close()

    preset = tmp_path / "s.yaml"
    preset.write_text(_SQLITE_PRESET, encoding="utf-8")
    output = tmp_path / "out.csv"

    result = process(db, preset, output, linked_entity="subject")
    assert result.row_counts["rows"] == 1
    row = _read_rows(output)[0]
    assert row["latitude_wgs84"] == "1.5"
    assert row["time_lower_unix_us"] == "1700000000000000"
