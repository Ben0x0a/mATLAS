"""Integration tests for the Container/discover/extract layer with synthetic fixtures.

Covers (no external acquisition needed):
- folder ``name`` fan-out (same basename in two subfolders -> preset applies to each);
- a zip whose db sits at ``filesystem1/private/.../db.sqlite`` matched by a prefix-tolerant
  ``path`` selector, with the WAL sibling staged and ``recovery_state`` surfaced;
- folder == zip parity for the same DB (identical canonical output);
- ``recovery_state`` is ``wal`` for WAL-only rows, ``live`` for committed rows, and never
  appears in the output CSV;
- §7.1 provenance (input_file_path full path, no ``input_file`` column, raw_source_path =
  inner path with prefix, traceability container_chain).
"""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath

from model_atlas.pipeline import process
from model_atlas.sources.container import FilesystemContainer, SourceFile, ZipContainer
from model_atlas.sources.readers.sqlite_reader import SqliteReader

# --- fixtures -------------------------------------------------------------

_DB_TABLE = "LOC"


def _make_wal_db(target_dir: Path, db_name: str = "db.sqlite") -> Path:
    """Create ``db_name`` with one committed row (-> main) and one WAL-only row, plus its
    live ``-wal`` sibling, inside ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    work = target_dir / "_work.sqlite"
    conn = sqlite3.connect(work)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(f"CREATE TABLE {_DB_TABLE} (id INTEGER, lat REAL, lon REAL, ts REAL)")
        conn.execute(f"INSERT INTO {_DB_TABLE} VALUES (1, 1.0, 2.0, 1700000000.0)")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")     # row 1 -> main file, WAL emptied
        conn.execute(f"INSERT INTO {_DB_TABLE} VALUES (2, 3.0, 4.0, 1700000100.0)")
        conn.commit()                                       # row 2 stays in the WAL
        db = target_dir / db_name
        shutil.copy2(work, db)
        shutil.copy2(work.with_name(work.name + "-wal"), db.with_name(db_name + "-wal"))
    finally:
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        work.with_name(work.name + suffix).unlink(missing_ok=True)
    return target_dir / db_name


_SQLITE_PRESET_PATH = """
preset: {{id: t.loc, name: Loc, version: 1.0, tier: primary}}
input_selector: {{format: sqlite, path: /private/var/db.sqlite, table: {table}}}
common: {{entity: const(device)}}
assertions:
  - position: {{latitude_wgs84: column(lat), longitude_wgs84: column(lon)}}
    time: {{instant: column(ts), epoch: unix_s}}
    links: {{entity_position: at, entity_time: observed_at, spatial_temporal: instant}}
""".format(table=_DB_TABLE)

_SQLITE_PRESET_NAME = _SQLITE_PRESET_PATH.replace(
    "path: /private/var/db.sqlite", "name: db.sqlite"
)

_CSV_PRESET = """
preset: {id: t.csv, name: Csv, version: 1.0, tier: secondary}
input_selector: {format: csv, name: loc.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def _zip_with_db(zip_path: Path, db: Path, root: str = "filesystem1/private/var") -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(db, f"{root}/db.sqlite")
        zf.write(db.with_name(db.name + "-wal"), f"{root}/db.sqlite-wal")


# --- reader-level recovery_state ------------------------------------------

def test_recovery_state_wal_vs_live(tmp_path: Path) -> None:
    db_dir = tmp_path / "folder" / "private" / "var"
    db = _make_wal_db(db_dir)
    container = FilesystemContainer(tmp_path / "folder")
    file = SourceFile(containers=(container,), logical_path=PurePosixPath("private/var/db.sqlite"))

    result = SqliteReader().read(file, {"table": _DB_TABLE})

    assert len(result.dataframe) == 2
    recovery = list(result.recovery_state)
    assert recovery == ["live", "wal"]                     # committed row, then WAL-only row
    assert "_meta_sqlite_source" not in result.dataframe.columns


# --- folder name fan-out --------------------------------------------------

def test_folder_name_fanout(tmp_path: Path) -> None:
    folder = tmp_path / "exports"
    for sub in ("a", "b"):
        d = folder / sub
        d.mkdir(parents=True)
        (d / "loc.csv").write_text("lat,lon,ts\n1.0,2.0,1700000000\n", encoding="utf-8")
    presets = tmp_path / "p.yaml"
    presets.write_text(_CSV_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(folder, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 2                  # the preset applied to each file
    assert len(result.matched) == 2


# --- zip path match + WAL sibling + recovery_state ------------------------

def test_zip_path_match_with_wal(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "EXTRACTION_FFS.zip"
    _zip_with_db(archive, db)
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(archive, presets, out, linked_entity="subj", traceability_format="prov")

    assert result.row_counts["rows"] == 2
    rows = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
    # §7.1: input_file_path is the FULL path of the outermost artifact = the input zip.
    assert rows[0]["input_file_path"] == str(archive)
    assert "input_file" not in rows[0]                     # the old column is gone
    # raw_source_path = inner path WITH prefix, WITHOUT the container name.
    assert rows[0]["raw_source_path"] == "filesystem1/private/var/db.sqlite"
    # recovery_state is captured on the enrichment, NOT written to the CSV.
    assert "recovery_state" not in rows[0]

    prov = json.loads(out.with_suffix(".matlas.traceability.json").read_text())
    source = prov["entity"]["matlas:source/0"]
    assert source["matlas:input_file_path"] == str(archive)
    assert source["matlas:container_chain"] == ["EXTRACTION_FFS.zip"]


# --- folder == zip parity -------------------------------------------------

def _canonical(rows: list[dict], drop: tuple[str, ...]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def test_folder_zip_parity(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")

    # zip source
    archive = tmp_path / "dump.zip"
    _zip_with_db(archive, db)
    zip_out = tmp_path / "zip.csv"
    process(archive, presets, zip_out, linked_entity="subj")

    # loose folder source at the same logical path
    folder = tmp_path / "loose"
    loose_dir = folder / "filesystem1" / "private" / "var"
    loose_dir.mkdir(parents=True)
    shutil.copy2(db, loose_dir / "db.sqlite")
    shutil.copy2(db.with_name(db.name + "-wal"), loose_dir / "db.sqlite-wal")
    folder_out = tmp_path / "folder.csv"
    process(folder, presets, folder_out, linked_entity="subj")

    zip_rows = list(csv.DictReader(zip_out.read_text(encoding="utf-8").splitlines()))
    folder_rows = list(csv.DictReader(folder_out.read_text(encoding="utf-8").splitlines()))
    # input_file_path differs (zip path vs loose db path); everything else is identical.
    drop = ("input_file_path",)
    assert _canonical(zip_rows, drop) == _canonical(folder_rows, drop)


def test_loose_file_container_chain_empty(tmp_path: Path) -> None:
    folder = tmp_path / "loose"
    (folder / "x").mkdir(parents=True)
    (folder / "x" / "loc.csv").write_text("lat,lon,ts\n1.0,2.0,1700000000\n", encoding="utf-8")
    presets = tmp_path / "p.yaml"
    presets.write_text(_CSV_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    process(folder, presets, out, linked_entity="subj", traceability_format="prov")
    prov = json.loads(out.with_suffix(".matlas.traceability.json").read_text())
    assert prov["entity"]["matlas:source/0"]["matlas:container_chain"] == []


# --- container depth: folder=0, archive input=1 ---------------------------

def test_folder_of_archives_is_descended(tmp_path: Path) -> None:
    """A folder is depth 0, so at the default depth the zip acquisitions inside it are
    opened and treated as sources (the bug: a folder of FFS zips must just work)."""
    db = _make_wal_db(tmp_path / "src")
    folder = tmp_path / "case"
    (folder / "devices").mkdir(parents=True)
    archive = folder / "devices" / "EXTRACTION_FFS.zip"
    _zip_with_db(archive, db)
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(folder, presets, out, linked_entity="subj", traceability_format="prov")

    assert result.row_counts["rows"] == 2                  # descended into the zip in the folder
    prov = json.loads(out.with_suffix(".matlas.traceability.json").read_text())
    source = prov["entity"]["matlas:source/0"]
    assert source["matlas:container_chain"] == ["EXTRACTION_FFS.zip"]
    assert source["matlas:input_file_path"] == str(archive)  # the zip, not the folder


def test_nested_archive_in_zip_not_explored_by_default(tmp_path: Path) -> None:
    """A direct zip is depth 1, so archives nested inside it are NOT explored at the
    default depth (nested-archive exploration stays off unless asked for)."""
    db = _make_wal_db(tmp_path / "src")
    inner = tmp_path / "inner.zip"
    _zip_with_db(inner, db)
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "filesystem1/private/var/inner.zip")
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(outer, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 0                  # inner.zip left unopened at depth 1
    assert result.matched == []
