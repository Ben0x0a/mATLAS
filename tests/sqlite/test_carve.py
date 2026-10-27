"""Tests for freelist + free-space carving (``model_atlas.sqlite.carve``).

Covers, over fixtures built in-process (no committed binary): deleted rows are recovered
from a leaf page's free space; dropped-table rows are recovered from freelist pages; each
carved row is tagged with the right freelist/freespace provenance; the ``SqliteReader``
surfaces carved rows as ``RecordState.RECOVERED`` numbered AFTER all live rows (rule 1);
carving is deterministic (a re-run is byte-identical); and a clean db with no unallocated
content adds no rows, so live-read behaviour is unchanged.

FALSE-POSITIVE note: the int-pk ``id`` column is NOT recovered on a deleted row — its
rowid varint sits in the cell framing a freeblock header overwrites — so carved rows are
identified by a sentinel value in a non-pk column (``hacc``), exactly as a forensic
analyst would treat a carved id column as unreliable.
"""
from __future__ import annotations

import logging
import sqlite3
import struct
from pathlib import Path

import pytest

from model_atlas.core.vocabularies import RecordState, RecoverySource
from model_atlas.ingest.readers.sqlite_reader import SqliteReader
from model_atlas.sqlite.carve import (
    CARVE_SOURCE_COLNAME,
    FREELIST_SOURCE_NAME,
    FREESPACE_SOURCE_NAME,
    _HEADER_MAGIC,
    _parse_file_header,
    carve_table,
)

_ROW = tuple[int, float, float, float]  # (id, ts, hacc, lat)
_CREATE = "CREATE TABLE locations (id INTEGER PRIMARY KEY, ts REAL, hacc REAL, lat REAL)"
_INSERT = "INSERT INTO locations(id, ts, hacc, lat) VALUES (?,?,?,?)"


def _db_with_deleted_rows(db_path: Path, kept: list[_ROW], deleted: list[_ROW]) -> Path:
    """Insert ``kept`` + ``deleted``, then DELETE the ``deleted`` ids.

    ``secure_delete=OFF`` is pinned so freed cell bytes survive on a host whose SQLite
    build defaults SECURE_DELETE on (which would zero them and leave nothing to carve);
    ``journal_mode=DELETE`` keeps everything in the main file (no WAL sidecar).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(_CREATE)
        conn.executemany(_INSERT, kept + deleted)
        conn.commit()
        ids = [r[0] for r in deleted]
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM locations WHERE id IN ({placeholders})", ids)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _db_with_freelist(db_path: Path, kept: list[_ROW], junk: int = 80) -> Path:
    """One live table plus a same-schema decoy table that is INSERTed then DROPped, so its
    (carve-compatible) pages land on the freelist with their bytes intact."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA secure_delete=OFF")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(_CREATE)
        conn.executemany(_INSERT, kept)
        conn.execute(
            "CREATE TABLE decoy (id INTEGER PRIMARY KEY, ts REAL, hacc REAL, lat REAL)"
        )
        decoy = [(9000 + i, 5000.0 + i, 77.7, 12.34) for i in range(junk)]
        conn.executemany(
            "INSERT INTO decoy(id, ts, hacc, lat) VALUES (?,?,?,?)", decoy
        )
        conn.commit()
        conn.execute("DROP TABLE decoy")
        conn.commit()
    finally:
        conn.close()
    return db_path


# --- carve_table (module level) ---------------------------------------------

def test_carve_recovers_deleted_rows_from_freespace(tmp_path: Path) -> None:
    kept = [(i, 1000.0 + i, 5.0, 10.0) for i in range(1, 80)]
    deleted = [(1000 + i, 9000.0 + i, 77.7, 12.34) for i in range(40)]
    db = _db_with_deleted_rows(tmp_path / "del.sqlite", kept, deleted)

    carved = carve_table(db, "locations")
    assert carved, "expected at least one carved row"
    sources = {row[CARVE_SOURCE_COLNAME] for row in carved}
    assert sources.issubset({FREELIST_SOURCE_NAME, FREESPACE_SOURCE_NAME})
    # The sentinel we placed on every deleted row must survive in at least one carved row.
    sentinels = [row for row in carved if row["hacc"] == 77.7]
    assert sentinels, f"no carved row carried the sentinel hacc=77.7; got {carved}"


def test_carve_recovers_dropped_table_rows_from_freelist(tmp_path: Path) -> None:
    db = _db_with_freelist(tmp_path / "free.sqlite", kept=[(1, 1.0, 5.0, 0.0)], junk=80)

    carved = carve_table(db, "locations")
    freelist_hits = [
        row for row in carved
        if row[CARVE_SOURCE_COLNAME] == FREELIST_SOURCE_NAME and row["ts"] and 5000.0 <= row["ts"] < 5100.0
    ]
    assert freelist_hits, f"expected decoy rows recovered from the freelist; got {carved}"


def test_carve_is_skipped_on_column_mismatch(tmp_path: Path) -> None:
    # A live column list that disagrees with the table's real columns => carve returns []
    # rather than emitting rows that would not line up with the live rows.
    db = _db_with_deleted_rows(
        tmp_path / "mm.sqlite", kept=[(1, 1.0, 5.0, 0.0)], deleted=[(2, 2.0, 77.7, 0.0)]
    )
    assert carve_table(db, "locations", columns=["id", "ts"]) == []


def test_carve_is_deterministic(tmp_path: Path) -> None:
    deleted = [(1000 + i, 9000.0 + i, 77.7, 12.34) for i in range(40)]
    db = _db_with_deleted_rows(
        tmp_path / "det.sqlite",
        kept=[(i, 1000.0 + i, 5.0, 10.0) for i in range(1, 80)],
        deleted=deleted,
    )
    first = carve_table(db, "locations")
    second = carve_table(db, "locations")
    assert first == second and first, "carve must be a byte-identical function of the file"


# --- SqliteReader end-to-end ------------------------------------------------

def test_reader_surfaces_carved_rows_recovered_after_live(tmp_path: Path) -> None:
    # The 79-kept / 40-deleted recipe reliably leaves carvable free-space cells (the
    # established fixture size); a much smaller delete set can be reclaimed with nothing
    # left to carve, which is expected best-effort carving behaviour, not a bug.
    kept = [(i, 1000.0 + i, 5.0, 10.0) for i in range(1, 80)]
    deleted = [(1000 + i, 9000.0 + i, 77.7, 12.34) for i in range(40)]
    db = _db_with_deleted_rows(tmp_path / "e2e.sqlite", kept, deleted)

    records = list(SqliteReader().iter_records(db, "locations"))
    live = [r for r in records if r.recovery_state == RecordState.LIVE]
    recovered = [r for r in records if r.recovery_state == RecordState.RECOVERED]

    # Live rows are exactly the kept rows, numbered first in a contiguous run.
    assert [r.fields["id"] for r in live] == [r[0] for r in kept]
    assert [r.line_no for r in live] == list(range(1, len(kept) + 1))
    # Recovered (carved) rows come AFTER all live rows, continuing the sequence.
    assert recovered, "expected carved rows to be recovered"
    assert min(r.line_no for r in recovered) == len(live) + 1
    assert [r.line_no for r in records] == list(range(1, len(records) + 1))
    # At least one carved row carries the deleted-row sentinel.
    assert any(r.fields.get("hacc") == 77.7 for r in recovered)
    # Provenance: live rows are LIVE; every carved row names its origin (freelist/freespace),
    # the fine recovery_source behind the coarse RECOVERED state.
    assert all(r.recovery_source is None for r in live)      # recovered from nowhere
    assert all(
        r.recovery_source in (RecoverySource.FREELIST, RecoverySource.FREESPACE)
        for r in recovered
    )


def test_reader_reread_is_identical_with_carving(tmp_path: Path) -> None:
    db = _db_with_deleted_rows(
        tmp_path / "rr.sqlite",
        kept=[(i, 1000.0 + i, 5.0, 10.0) for i in range(1, 80)],
        deleted=[(1000 + i, 9000.0 + i, 77.7, 12.34) for i in range(40)],
    )
    first = [
        (r.line_no, r.recovery_state, tuple(sorted(r.fields.items(), key=lambda kv: kv[0])))
        for r in SqliteReader().iter_records(db, "locations")
    ]
    second = [
        (r.line_no, r.recovery_state, tuple(sorted(r.fields.items(), key=lambda kv: kv[0])))
        for r in SqliteReader().iter_records(db, "locations")
    ]
    assert first == second


def test_reader_carve_opt_out(tmp_path: Path) -> None:
    db = _db_with_deleted_rows(
        tmp_path / "off.sqlite",
        kept=[(i, 1000.0 + i, 5.0, 10.0) for i in range(1, 40)],
        deleted=[(1000 + i, 9000.0 + i, 77.7, 12.34) for i in range(30)],
    )
    records = list(SqliteReader().iter_records(db, "locations", params={"carve": False}))
    assert all(r.recovery_state == RecordState.LIVE for r in records)
    assert [r.fields["id"] for r in records] == list(range(1, 40))


def test_reader_clean_db_has_no_carved_rows(tmp_path: Path) -> None:
    # No deletes, no drops => no unallocated content => carving adds nothing, so the live
    # read is unchanged even with carving on by default.
    db = tmp_path / "clean.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(_CREATE)
    conn.executemany(_INSERT, [(1, 1.0, 5.0, 0.0), (2, 2.0, 6.0, 1.0)])
    conn.commit()
    conn.close()

    records = list(SqliteReader().iter_records(db, "locations"))
    assert [r.recovery_state for r in records] == [RecordState.LIVE, RecordState.LIVE]
    assert [r.fields["id"] for r in records] == [1, 2]


# --- FIX-01: a carve failure must never abort the live read -----------------

def test_reader_carve_failure_keeps_all_live_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A table whose name contains a `"` makes carve's strict identifier quoting RAISE
    # (the live read tolerates it via quote-doubling). The carve pass must degrade to
    # "no carved rows" — with a logged warning — while EVERY live row is still yielded.
    db = tmp_path / "quoted.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute('CREATE TABLE "we""ird" (id INTEGER, v TEXT)')
        conn.executemany('INSERT INTO "we""ird" VALUES (?, ?)', [(1, "a"), (2, "b"), (3, "c")])
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level(logging.WARNING):
        records = list(SqliteReader().iter_records(db, 'we"ird'))

    assert [r.fields["id"] for r in records] == [1, 2, 3]  # ALL live rows survived
    assert all(r.recovery_state == RecordState.LIVE for r in records)
    assert any("carve pass failed" in message for message in caplog.messages)


def _header_with_page_size(page_size: int) -> bytes:
    """A 100-byte SQLite database header carrying ``page_size`` at bytes 16:18."""
    header = bytearray(100)
    header[: len(_HEADER_MAGIC)] = _HEADER_MAGIC
    struct.pack_into(">H", header, 16, page_size)
    return bytes(header)


def test_parse_file_header_page_size_zero_raises_valueerror_not_zerodivision() -> None:
    # A crafted header with page_size == 0 must NOT ZeroDivision the total-pages maths;
    # it degrades to a clean, typed ValueError (which carve_table's caller isolates).
    with pytest.raises(ValueError):
        _parse_file_header(_header_with_page_size(0))
