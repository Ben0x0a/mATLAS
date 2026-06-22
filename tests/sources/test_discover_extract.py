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

import pytest
from openpyxl import Workbook

from model_atlas.pipeline import process
from model_atlas.sources.container import FilesystemContainer, SourceFile, ZipContainer
from model_atlas.sources.readers.excel_reader import ExcelReader
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


# --- excel reader ---------------------------------------------------------

def _make_xlsx(path: Path, sheets: dict[str, tuple[list, list[list]]]) -> Path:
    wb = Workbook()
    first = True
    for name, (header, rows) in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
        ws.append(header)
        for row in rows:
            ws.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


_EXCEL_PRESET = """
preset: {id: t.xl, name: Xl, version: 1.0, tier: secondary}
input_selector: {format: excel, name: book.xlsx, sheet: Trips}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_excel_reader_reads_named_sheet(tmp_path: Path) -> None:
    book = _make_xlsx(tmp_path / "in" / "book.xlsx", {
        "Other": (["x"], [[1]]),
        "Trips": (["lat", "lon", "ts"], [[1.0, 2.0, 1700000000], [3.0, 4.0, 1700000100]]),
    })
    presets = tmp_path / "p.yaml"
    presets.write_text(_EXCEL_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    # folder input -> matched by name (not force mode); the named sheet drives extraction.
    result = process(book.parent, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 2
    file = SourceFile(containers=(FilesystemContainer(book.parent),),
                      logical_path=PurePosixPath("book.xlsx"))
    res = ExcelReader().read(file, {"sheet": "Trips"})
    assert list(res.recovery_state) == ["live", "live"]    # excel rows are always live
    assert set(ExcelReader().list_subtables(file)) == {"Other", "Trips"}


def test_force_mode_excel_sheet_fallback_by_columns(tmp_path: Path) -> None:
    """Force mode ignores the (absent) declared sheet and picks the sheet whose columns
    satisfy the preset's mapping."""
    book = _make_xlsx(tmp_path / "book.xlsx", {
        "Sheet1": (["foo", "bar"], [[1, 2]]),
        "Renamed": (["lat", "lon", "ts"], [[1.0, 2.0, 1700000000]]),
    })
    presets = tmp_path / "p.yaml"
    presets.write_text(_EXCEL_PRESET, encoding="utf-8")   # declares sheet: Trips (absent)
    out = tmp_path / "out.csv"

    # single file + single preset -> force mode; sheet "Trips" missing -> fallback to "Renamed".
    result = process(book, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 1


# --- multi-selector OR / multi-role AND -----------------------------------

_OR_PRESET = """
preset: {id: t.or, name: Or, version: 1.0, tier: secondary}
input_selector:
  - {format: csv, name: primary.csv}
  - {format: csv, name: backup.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_MULTIROLE_PRESET = """
preset: {id: t.multi, name: Multi, version: 1.0, tier: secondary}
input_selector:
  - {role: primary, format: csv, name: a.csv}
  - {role: account, format: csv, name: b.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_multi_selector_or_matches_second_alternative(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "backup.csv").write_text("lat,lon,ts\n1.0,2.0,1700000000\n", encoding="utf-8")
    presets = tmp_path / "p.yaml"
    presets.write_text(_OR_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(folder, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 1                  # matched via the 2nd OR alternative
    assert len(result.matched) == 1


def test_multi_role_preset_raises_not_implemented(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "a.csv").write_text("lat,lon,ts\n1.0,2.0,1700000000\n", encoding="utf-8")
    presets = tmp_path / "p.yaml"
    presets.write_text(_MULTIROLE_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    with pytest.raises(NotImplementedError):
        process(folder, presets, out, linked_entity="subj")


# --- ZipContainer handle hygiene ------------------------------------------

def test_zip_open_closes_parent_handle(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "a.zip"
    _zip_with_db(archive, db)
    container = ZipContainer(path=archive)
    file = next(f for f in container.files() if f.name == "db.sqlite")

    stream = container.open(file)
    stream.read(16)
    zf = stream._zf
    stream.close()

    assert zf.fp is None                                   # parent ZipFile closed, not leaked


# --- root-prefix tolerance: the original bug, locked end-to-end -----------

def _zip_dbs(archive: Path, db: Path, arcnames: list[str]) -> None:
    """Write the same db (+ its WAL sibling) into a zip at each given internal path."""
    wal = db.with_name(db.name + "-wal")
    with zipfile.ZipFile(archive, "w") as zf:
        for arcname in arcnames:
            zf.write(db, arcname)
            zf.write(wal, arcname + "-wal")


@pytest.mark.parametrize("root", [
    "private/var",                  # no wrapper      (skip 0)
    "filesystem1/private/var",      # GrayKey-style   (skip 1)
    "_/private/var",               # "_" wrapper     (skip 1)
    "Dump/private/var",             # arbitrary tool root (skip 1)
])
def test_root_prefix_tolerance_in_zip_is_never_a_silent_miss(tmp_path: Path, root: str) -> None:
    """REGRESSION (the bug this session started from): a selector ``path: /private/var/
    db.sqlite`` must match the same logical file inside a zip regardless of the tool's
    root-folder wrapper (``_``, ``filesystem1``, ``Dump``, or none). A miss here used to
    be silent (folder matched, zip did not)."""
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "acq.zip"
    _zip_dbs(archive, db, [f"{root}/db.sqlite"])
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")   # path: /private/var/db.sqlite
    out = tmp_path / "out.csv"

    result = process(archive, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 2, f"prefix {root!r} silently failed to match"
    assert result.unmatched == []


def test_overdeep_prefix_is_a_clean_miss_then_matches_with_depth(tmp_path: Path) -> None:
    """A 2-segment wrapper is NOT silently matched at the default depth (so we never
    grab the wrong file), but the ``root_prefix_depth`` knob opens it deliberately."""
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "acq.zip"
    _zip_dbs(archive, db, ["vol/filesystem1/private/var/db.sqlite"])
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")

    shallow = process(archive, presets, tmp_path / "a.csv", linked_entity="subj")
    assert shallow.row_counts["rows"] == 0                  # clean miss, not silent mismatch
    assert len(shallow.unmatched) == 1                     # reported as unmatched, not dropped

    deep = process(archive, presets, tmp_path / "b.csv", linked_entity="subj", root_prefix_depth=2)
    assert deep.row_counts["rows"] == 2                     # the knob opens the deeper wrapper


# --- {uuid} and * path wildcards (the app-data case) ----------------------

def test_uuid_path_fans_out_across_app_containers(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    base = "filesystem1/private/var/mobile/Containers/Data/Application"
    archive = tmp_path / "ffs.zip"
    _zip_dbs(archive, db, [
        f"{base}/005DDA28-C17A-4079-BBB6-E6255870D163/Documents/db.sqlite",
        f"{base}/10C21048-504F-465B-BA34-E521CE9CABA7/Documents/db.sqlite",
    ])
    preset = _SQLITE_PRESET_PATH.replace(
        "path: /private/var/db.sqlite",
        'path: "/private/var/mobile/Containers/Data/Application/{uuid}/Documents/db.sqlite"',
    )
    presets = tmp_path / "p.yaml"
    presets.write_text(preset, encoding="utf-8")
    out = tmp_path / "out.csv"

    result = process(archive, presets, out, linked_entity="subj")

    assert result.row_counts["rows"] == 4                  # 2 rows x 2 GUID containers (fan-out)
    assert len(result.matched) == 2


def test_uuid_token_does_not_match_non_uuid_segment(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "ffs.zip"
    _zip_dbs(archive, db, ["filesystem1/private/var/mobile/Containers/Data/Application/NOTAUUID/Documents/db.sqlite"])
    preset = _SQLITE_PRESET_PATH.replace(
        "path: /private/var/db.sqlite",
        'path: "/private/var/mobile/Containers/Data/Application/{uuid}/Documents/db.sqlite"',
    )
    presets = tmp_path / "p.yaml"
    presets.write_text(preset, encoding="utf-8")

    result = process(archive, presets, tmp_path / "out.csv", linked_entity="subj")
    assert result.row_counts["rows"] == 0                  # {uuid} must reject a non-UUID segment


def test_star_wildcard_matches_one_variable_segment(tmp_path: Path) -> None:
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "ffs.zip"
    _zip_dbs(archive, db, ["filesystem1/private/var/AppABC/db.sqlite"])
    preset = _SQLITE_PRESET_PATH.replace(
        "path: /private/var/db.sqlite", "path: /private/var/*/db.sqlite")
    presets = tmp_path / "p.yaml"
    presets.write_text(preset, encoding="utf-8")

    result = process(archive, presets, tmp_path / "out.csv", linked_entity="subj")
    assert result.row_counts["rows"] == 2


# --- multi-selector OR for sqlite (FFS path vs iTunes-backup hash name) ----

def test_or_selector_matches_backup_hash_name(tmp_path: Path) -> None:
    """sms.db-style: one logical source, two provenances — an FFS path OR a flat
    backup file named by its domain hash. A file matching the name alternative extracts."""
    db = _make_wal_db(tmp_path / "src")
    hashed = "3d0d7e5fb2ce288813306e4d4636395e047a3d28"
    archive = tmp_path / "backup.zip"
    _zip_dbs(archive, db, [hashed])                        # flat hashed entry, no extension
    preset = (
        _SQLITE_PRESET_PATH
        .replace(
            "input_selector: {format: sqlite, path: /private/var/db.sqlite, table: LOC}",
            "input_selector:\n"
            "  - {format: sqlite, path: /private/var/sms.db, table: LOC}\n"
            f"  - {{format: sqlite, name: {hashed}, table: LOC}}\n",
        )
    )
    presets = tmp_path / "p.yaml"
    presets.write_text(preset, encoding="utf-8")

    result = process(archive, presets, tmp_path / "out.csv", linked_entity="subj")
    assert result.row_counts["rows"] == 2                  # matched via the backup-name alternative


# --- force mode: sqlite table fallback by columns -------------------------

def test_force_mode_sqlite_table_fallback_by_columns(tmp_path: Path) -> None:
    """Force ignores location but resolves the table: when the declared table is absent,
    it falls back to the table carrying the mapped columns."""
    db = tmp_path / "loose.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE renamed (id INTEGER, lat REAL, lon REAL, ts REAL)")
        conn.execute("INSERT INTO renamed VALUES (1, 1.0, 2.0, 1700000000.0)")
        conn.commit()
    finally:
        conn.close()
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")   # declares table LOC (absent)
    out = tmp_path / "out.csv"

    # single sqlite file + single preset -> force mode; table LOC missing -> "renamed".
    result = process(db, presets, out, linked_entity="subj")
    assert result.row_counts["rows"] == 1


# --- container-depth matrix: the knob, folder-of-zips, folder->zip->nested -

def test_folder_with_multiple_zips_all_descended(tmp_path: Path) -> None:
    """A case folder holding several acquisition zips: every one is opened (depth 0->1)."""
    db = _make_wal_db(tmp_path / "src")
    folder = tmp_path / "case"
    (folder / "d1").mkdir(parents=True)
    (folder / "d2").mkdir(parents=True)
    _zip_dbs(folder / "d1" / "a.zip", db, ["filesystem1/private/var/db.sqlite"])
    _zip_dbs(folder / "d2" / "b.zip", db, ["filesystem1/private/var/db.sqlite"])
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")

    result = process(folder, presets, tmp_path / "out.csv", linked_entity="subj")

    assert result.row_counts["rows"] == 4                  # 2 rows x 2 zips
    assert len(result.matched) == 2


def test_direct_zip_nested_archive_explored_at_depth_2(tmp_path: Path) -> None:
    """The ``max_container_depth`` knob deliberately opens a zip-inside-a-zip (depth 1->2)."""
    db = _make_wal_db(tmp_path / "src")
    inner = tmp_path / "inner.zip"
    _zip_dbs(inner, db, ["filesystem1/private/var/db.sqlite"])
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.write(inner, "inner.zip")
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")

    default = process(outer, presets, tmp_path / "a.csv", linked_entity="subj")
    assert default.row_counts["rows"] == 0                 # inner.zip unopened at depth 1

    deep = process(outer, presets, tmp_path / "b.csv", linked_entity="subj", max_container_depth=2)
    assert deep.row_counts["rows"] == 2                    # opened at depth 2


def test_folder_zip_nested_needs_depth_2(tmp_path: Path) -> None:
    """folder(0) -> outer.zip(1) -> inner.zip(2): the innermost db is out of reach at the
    default depth and reached only when the budget allows the second archive level."""
    db = _make_wal_db(tmp_path / "src")
    inner = tmp_path / "inner.zip"
    _zip_dbs(inner, db, ["filesystem1/private/var/db.sqlite"])
    folder = tmp_path / "case"
    folder.mkdir()
    with zipfile.ZipFile(folder / "outer.zip", "w") as z:
        z.write(inner, "inner.zip")
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")

    default = process(folder, presets, tmp_path / "a.csv", linked_entity="subj")
    assert default.row_counts["rows"] == 0                 # folder(0)->outer(1), inner(2) not reached

    deep = process(folder, presets, tmp_path / "b.csv", linked_entity="subj", max_container_depth=2)
    assert deep.row_counts["rows"] == 2                    # folder(0)->outer(1)->inner(2)


# --- §7.1 traceability detail keys ----------------------------------------

def test_traceability_emits_all_detail_keys(tmp_path: Path) -> None:
    """Every §7.1 per-source provenance key is populated (not just input_file_path /
    container_chain): format, table/sheet, fingerprint, names, and inner path."""
    db = _make_wal_db(tmp_path / "src")
    archive = tmp_path / "EXTRACTION_FFS.zip"
    _zip_with_db(archive, db)
    presets = tmp_path / "p.yaml"
    presets.write_text(_SQLITE_PRESET_PATH, encoding="utf-8")
    out = tmp_path / "out.csv"

    process(archive, presets, out, linked_entity="subj", traceability_format="prov")

    src = json.loads(out.with_suffix(".matlas.traceability.json").read_text())["entity"]["matlas:source/0"]
    assert src["matlas:raw_source_path"] == "filesystem1/private/var/db.sqlite"
    assert src["matlas:input_file_path"] == str(archive)
    assert src["matlas:input_file_name"] == "EXTRACTION_FFS.zip"
    assert src["matlas:container_chain"] == ["EXTRACTION_FFS.zip"]
    assert src["matlas:format"] == "sqlite"
    assert src["matlas:table"] == "LOC"
    assert src["matlas:sheet"] is None
    assert isinstance(src["matlas:source_fingerprint"], str) and len(src["matlas:source_fingerprint"]) == 64
