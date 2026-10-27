"""Tests for the ingest ``Reader`` protocol implementations: csv/excel/sqlite/xml.

Covers: normative enumeration order per format (core/determinism.py rules 1-3), verbatim
values (a float-precision CSV value survives byte-for-byte), the sqlite
``peek_fields`` security fix (a table name containing ``"`` never reaches SQL), and
determinism (re-reading the same file yields an identical ``line_no`` sequence).
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from model_atlas.core.vocabularies import RecordState, RecoverySource
from model_atlas.ingest.readers.csv_reader import CsvReader
from model_atlas.ingest.readers.excel_reader import ExcelReader
from model_atlas.ingest.readers.sqlite_reader import SqliteReader, _sidecar_recovery_source

pytest.importorskip("openpyxl")
from openpyxl import Workbook  # noqa: E402


def _xlsx(path: Path, sheets: dict[str, tuple[list, list[list]]]) -> Path:
    wb = Workbook()
    first = True
    for name, (header, rows) in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
        ws.append(header)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def _db(path: Path, table: str = "t", rows: int = 2) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER, lat REAL, lon REAL)")
        for i in range(rows):
            conn.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (i, 1.0 * i, 2.0 * i))
        conn.commit()
    finally:
        conn.close()
    return path


def _wal_db(target_dir: Path, db_name: str = "db.sqlite", table: str = "t") -> Path:
    """One committed row (-> main) + one WAL-only row, plus its live ``-wal`` sibling.

    Mirrors tests/sources/test_discover_extract.py's ``_make_wal_db`` fixture recipe:
    checkpoint after the first commit (row -> main, WAL emptied), then a second commit
    that stays WAL-only, and the WAL sibling is copied out BEFORE the connection closes
    (closing checkpoints/removes it otherwise).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    work = target_dir / "_work.sqlite"
    conn = sqlite3.connect(work)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(f"CREATE TABLE {table} (id INTEGER, v TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES (1, 'live')")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute(f"INSERT INTO {table} VALUES (2, 'recovered')")
        conn.commit()
        db = target_dir / db_name
        shutil.copy2(work, db)
        shutil.copy2(work.with_name(work.name + "-wal"), db.with_name(db_name + "-wal"))
    finally:
        conn.close()
    for suffix in ("", "-wal", "-shm"):
        work.with_name(work.name + suffix).unlink(missing_ok=True)
    return target_dir / db_name


# --- CSV --------------------------------------------------------------------

def test_csv_reader_honours_delimiter(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("a;b\n1;2\n3;4\n", encoding="utf-8")
    recs = list(CsvReader().iter_records(tmp_path / "d.csv", None, {"delimiter": ";"}))
    assert [r.fields for r in recs] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    assert [r.line_no for r in recs] == [1, 2]
    assert all(r.recovery_state == RecordState.LIVE for r in recs)


def test_csv_reader_skiprows(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("junk line\na,b\n1,2\n", encoding="utf-8")
    recs = list(CsvReader().iter_records(tmp_path / "d.csv", None, {"skip_rows": 1}))
    assert len(recs) == 1
    assert recs[0].fields == {"a": "1", "b": "2"}


def test_csv_blank_line_gets_a_line_no_header_excluded(tmp_path: Path) -> None:
    # Rule 2: header excluded from numbering, blank physical lines counted (not skipped).
    path = tmp_path / "d.csv"
    path.write_text("a,b\n1,2\n\n3,4\n", encoding="utf-8")
    recs = list(CsvReader().iter_records(path, None))
    assert [r.line_no for r in recs] == [1, 2, 3]
    assert recs[1].fields == {"a": None, "b": None}  # the blank line: no fields, still counted


def test_csv_float_precision_survives_verbatim(tmp_path: Path) -> None:
    # canonical JSON rule 5: no float re-formatting; "46.193000" must not become 46.193.
    path = tmp_path / "d.csv"
    path.write_text("id,lat\n1,46.193000\n", encoding="utf-8")
    [record] = list(CsvReader().iter_records(path, None))
    assert record.fields["lat"] == "46.193000"


def test_csv_peek_fields_and_no_subunits(tmp_path: Path) -> None:
    (tmp_path / "d.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    reader = CsvReader()
    assert reader.peek_fields(tmp_path / "d.csv") == {"a", "b", "c"}
    assert reader.subunits(tmp_path / "d.csv") == []


def test_csv_rejects_a_subunit() -> None:
    with pytest.raises(ValueError):
        next(CsvReader().iter_records(Path("unused"), "sheet1"))


def test_csv_bom_does_not_corrupt_the_first_column_name(tmp_path: Path) -> None:
    # FIX-08 #1: an Excel-exported CSV starts with a UTF-8 BOM; it must be stripped, not glued
    # to the first header cell, so iter_records and peek_fields agree on column 1's name.
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfId,Name\n1,Alice\n")
    [record] = list(CsvReader().iter_records(path, None))
    assert record.fields == {"Id": "1", "Name": "Alice"}     # "Id", never "﻿Id"
    assert CsvReader().peek_fields(path) == {"Id", "Name"}   # the two paths now agree


def test_csv_quoted_control_char_not_split_into_phantom_lines(tmp_path: Path) -> None:
    # FIX-08 #7: a quoted field containing a vertical tab (\x0b) must stay ONE record. The old
    # str.splitlines() split on it and fabricated a phantom physical line, mis-parsing the row.
    path = tmp_path / "vt.csv"
    path.write_bytes(b'a,b\n"x\x0by",2\n')
    recs = list(CsvReader().iter_records(path, None))
    assert len(recs) == 1
    assert recs[0].fields == {"a": "x\x0by", "b": "2"}
    assert recs[0].line_no == 1


# --- Excel --------------------------------------------------------------------

def test_excel_reader_reads_requested_sheet_in_sheet_row_order(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {
        "S1": (["x"], [[1]]),
        "Data": (["a", "b"], [[1, 2], [3, 4]]),
    })
    recs = list(ExcelReader().iter_records(tmp_path / "b.xlsx", "Data"))
    assert [r.fields for r in recs] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    # Rule 3: line_no is the SHEET row number — header on row 1, so data starts at row 2.
    assert [r.line_no for r in recs] == [2, 3]
    assert all(r.recovery_state == RecordState.LIVE for r in recs)


def test_excel_peek_fields_and_subunits(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {"S1": (["x"], [[1]]), "Data": (["a", "b"], [[1, 2]])})
    reader = ExcelReader()
    assert set(reader.subunits(tmp_path / "b.xlsx")) == {"S1", "Data"}
    assert reader.peek_fields(tmp_path / "b.xlsx", "Data") == {"a", "b"}
    assert reader.peek_fields(tmp_path / "b.xlsx", None) is None


def test_excel_requires_a_sheet(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {"Data": (["a"], [[1]])})
    with pytest.raises(ValueError):
        next(ExcelReader().iter_records(tmp_path / "b.xlsx", None))


def test_excel_mid_blank_row_keeps_the_true_sheet_line_no(tmp_path: Path) -> None:
    # FIX-08 #4: line_no is the TRUE sheet row. A blank row keeps ITS number (3) and shifts
    # nothing — not an enumerate offset over a frame that might collapse the blank away.
    _xlsx(tmp_path / "b.xlsx", {"Data": (["a", "b"], [[1, 2], [None, None], [3, 4]])})
    recs = list(ExcelReader().iter_records(tmp_path / "b.xlsx", "Data"))
    assert [r.line_no for r in recs] == [2, 3, 4]
    assert recs[0].fields == {"a": "1", "b": "2"}
    assert recs[1].fields == {"a": "", "b": ""}      # the mid blank row: empty cells, still present
    assert recs[2].fields == {"a": "3", "b": "4"}


# --- SQLite ---------------------------------------------------------------

def test_sqlite_reader_table_order(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    recs = list(SqliteReader().iter_records(tmp_path / "x.sqlite", "t"))
    assert [r.fields["id"] for r in recs] == [0, 1]   # rowid order (rule 1)
    assert [r.line_no for r in recs] == [1, 2]
    assert all(r.recovery_state == RecordState.LIVE for r in recs)


def test_sqlite_subunits_and_peek(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    reader = SqliteReader()
    assert "t" in reader.subunits(tmp_path / "x.sqlite")
    assert reader.peek_fields(tmp_path / "x.sqlite", "t") == {"id", "lat", "lon"}


def test_sqlite_sql_query_path(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    recs = list(
        SqliteReader().iter_records(
            tmp_path / "x.sqlite", None, {"sql": "SELECT id, lat FROM t WHERE id >= 0"},
        )
    )
    assert len(recs) == 2
    assert {c.split(".")[-1] for c in recs[0].fields} == {"id", "lat"}


def test_sqlite_requires_exactly_one_of_table_or_sql(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    with pytest.raises(ValueError):
        next(SqliteReader().iter_records(tmp_path / "x.sqlite", None))               # neither
    with pytest.raises(ValueError):
        next(SqliteReader().iter_records(tmp_path / "x.sqlite", "t", {"sql": "SELECT 1"}))  # both


def test_sqlite_recovered_rows_come_after_live_continuing_line_no(tmp_path: Path) -> None:
    # Rule 1: recovered (WAL-only) rows enumerate AFTER all live rows, continuing line_no.
    db = _wal_db(tmp_path / "src")
    recs = list(SqliteReader().iter_records(db, "t"))
    assert [(r.fields["id"], r.recovery_state) for r in recs] == [
        (1, RecordState.LIVE),
        (2, RecordState.RECOVERED),
    ]
    assert [r.line_no for r in recs] == [1, 2]
    # The fine origin: the live row is LIVE, the WAL-only row names its sidecar (WAL).
    # A live row was recovered from nowhere, so it carries NO recovery_source: a
    # populated cell IS the "recovered" signal, never an echo of recovery_state.
    assert [r.recovery_source for r in recs] == [None, RecoverySource.WAL]


def test_sidecar_recovery_source_mapping() -> None:
    # wal/journal map to their RecoverySource; the unnamed-sidecar fallback is UNKNOWN
    # (a recovered row of indeterminate origin — never silently mislabelled LIVE).
    assert _sidecar_recovery_source("wal") == RecoverySource.WAL
    assert _sidecar_recovery_source("journal") == RecoverySource.JOURNAL
    assert _sidecar_recovery_source("sidecar") == RecoverySource.UNKNOWN


def test_sqlite_peek_fields_rejects_quoted_table_name(tmp_path: Path) -> None:
    # T90 carry-over: a table name carrying a double quote must be refused by
    # quote_identifier, so peek_fields returns None ("cannot verify") rather than
    # interpolating it into the PRAGMA or crashing.
    _db(tmp_path / "x.sqlite")
    reader = SqliteReader()
    malicious = 't" ) ; DROP TABLE t ; --'
    assert reader.peek_fields(tmp_path / "x.sqlite", malicious) is None
    # The table must genuinely be untouched (never executed).
    assert "t" in reader.subunits(tmp_path / "x.sqlite")


def test_sqlite_peek_fields_none_without_subunit(tmp_path: Path) -> None:
    _db(tmp_path / "x.sqlite")
    assert SqliteReader().peek_fields(tmp_path / "x.sqlite", None) is None


def test_sqlite_blob_cell_becomes_the_r18_envelope(tmp_path: Path) -> None:
    # Rule 7: a binary cell becomes {"$blob": {"sha256": ..., "bytes": n}} in fields, never
    # the raw bytes themselves.
    path = tmp_path / "x.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER, payload BLOB)")
    conn.execute("INSERT INTO t VALUES (1, ?)", (b"\x00\x01\xff",))
    conn.commit()
    conn.close()
    [record] = list(SqliteReader().iter_records(path, "t"))
    envelope = record.fields["payload"]
    assert set(envelope) == {"$blob"}
    assert envelope["$blob"]["bytes"] == 3
    assert len(envelope["$blob"]["sha256"]) == 64


# --- determinism: same file read twice -> identical line_no sequences -------

def test_csv_reread_yields_identical_line_no_sequence(tmp_path: Path) -> None:
    path = tmp_path / "d.csv"
    path.write_text("a,b\n1,2\n\n3,4\n5,6\n", encoding="utf-8")
    first = [r.line_no for r in CsvReader().iter_records(path, None)]
    second = [r.line_no for r in CsvReader().iter_records(path, None)]
    assert first == second == [1, 2, 3, 4]


def test_excel_reread_yields_identical_line_no_sequence(tmp_path: Path) -> None:
    _xlsx(tmp_path / "b.xlsx", {"Data": (["a", "b"], [[1, 2], [3, 4], [5, 6]])})
    first = [r.line_no for r in ExcelReader().iter_records(tmp_path / "b.xlsx", "Data")]
    second = [r.line_no for r in ExcelReader().iter_records(tmp_path / "b.xlsx", "Data")]
    assert first == second == [2, 3, 4]


def test_sqlite_reread_yields_identical_line_no_sequence(tmp_path: Path) -> None:
    db = _wal_db(tmp_path / "src")
    first = [(r.line_no, r.recovery_state) for r in SqliteReader().iter_records(db, "t")]
    second = [(r.line_no, r.recovery_state) for r in SqliteReader().iter_records(db, "t")]
    assert first == second


# --- ZipContainer: duplicate-path entries (FIX-08 #2) -------------------------

@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_duplicate_zip_entries_each_resolve_to_their_own_bytes(tmp_path: Path) -> None:
    import hashlib
    import io
    import zipfile

    from model_atlas.ingest.containers import ZipContainer

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("dup.csv", b"first")
        zf.writestr("dup.csv", b"second")     # a SECOND member with the SAME normalised path (intended)
    zip_path = tmp_path / "d.zip"
    zip_path.write_bytes(buffer.getvalue())

    container = ZipContainer(path=zip_path)
    files = list(container.files())
    assert len(files) == 2                     # both entries enumerated, neither shadowed
    hashes = [container.content_sha256(file) for file in files]
    # Each duplicate-path entry resolves to its OWN bytes (via entry_index), not both to the
    # first name-match — so the second file's bytes are never silently dropped.
    assert hashes == [hashlib.sha256(b"first").hexdigest(),
                      hashlib.sha256(b"second").hexdigest()]
