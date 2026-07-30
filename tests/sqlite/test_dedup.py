"""Tests for the db-only / db+sidecars merge (``model_atlas.sqlite.dedup``).

Covers the FIX-20 dedup redesign: observation semantics (two genuinely-distinct rows with
identical column values BOTH survive — never coalesced), the true cross-pass WAL overlap
still merged (a committed row re-read by the sidecar pass counts once), and dtype stability
(a NULL-driven int64->float64 promotion cannot make two large integers collide). Exercised
both at the module level (``merge_with_source``) and end-to-end through ``SqliteReader``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.sqlite_reader import SqliteReader
from model_atlas.sqlite.dedup import (
    SOURCE_COLNAME,
    merge_with_source,
)

# A frame built as the reader builds it: object dtype so an integer column carrying a NULL
# is not promoted to float64 (which would lose precision and collapse look-alike values).
_OBJ = "object"


def _frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns, dtype=_OBJ)


# --- observation semantics: no within-view dedup ----------------------------

def test_two_identical_valued_db_rows_both_survive() -> None:
    # Two byte-identical committed rows (e.g. two identical GPS pings) are two independent
    # observations: BOTH must be emitted and labelled live ('both'), never coalesced.
    cols = ["a", "b"]
    df = _frame([{"a": "x", "b": 1}, {"a": "x", "b": 1}], cols)
    merged = merge_with_source(df, df, sidecar_label="wal")
    assert len(merged) == 2
    assert list(merged[SOURCE_COLNAME]) == ["both", "both"]


def test_reader_yields_two_records_for_two_identical_rows(tmp_path: Path) -> None:
    db = tmp_path / "dup.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (a TEXT, b REAL)")
        conn.executemany("INSERT INTO t VALUES (?, ?)", [("x", 1.0), ("x", 1.0)])
        conn.commit()
    finally:
        conn.close()

    records = list(SqliteReader().iter_records(db, "t"))
    assert len(records) == 2  # both numbered, neither dropped
    assert [r.line_no for r in records] == [1, 2]
    assert all(r.recovery_state == RecordState.LIVE for r in records)
    assert [r.fields for r in records] == [{"a": "x", "b": 1.0}, {"a": "x", "b": 1.0}]


def test_wal_only_duplicate_rows_both_recovered() -> None:
    # db-only has one committed row; the sidecar pass re-reads it AND adds two identical
    # WAL-only rows. The committed row counts once ('both'); BOTH WAL rows are recovered.
    cols = ["v"]
    df_db = _frame([{"v": "c"}], cols)
    df_side = _frame([{"v": "c"}, {"v": "w"}, {"v": "w"}], cols)
    merged = merge_with_source(df_db, df_side, sidecar_label="wal")
    labels = list(merged[SOURCE_COLNAME])
    assert labels.count("both") == 1
    assert labels.count("wal") == 2  # duplicate WAL rows preserved, not collapsed


# --- true cross-pass overlap still merged -----------------------------------

def test_committed_row_reread_by_sidecar_counts_once() -> None:
    cols = ["v"]
    df_db = _frame([{"v": "c"}], cols)
    df_side = _frame([{"v": "c"}, {"v": "w"}], cols)  # re-reads 'c', adds WAL-only 'w'
    merged = merge_with_source(df_db, df_side, sidecar_label="wal")
    assert list(merged[SOURCE_COLNAME]) == ["both", "wal"]  # 'c' once, 'w' recovered


# --- dtype stability: large int + NULL, no collision ------------------------

def test_large_int_with_null_does_not_collide(tmp_path: Path) -> None:
    # 2**53 and 2**53+1 are indistinguishable as float64. A NULL in the same column would
    # promote it int64->float64 under naive construction, collapsing the two distinct rows.
    # Object dtype + a dtype-stable fingerprint keep them apart and exact.
    lo, hi = 9007199254740992, 9007199254740993  # 2**53, 2**53 + 1
    db = tmp_path / "big.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE t (id INTEGER, big INTEGER)")
        conn.executemany(
            "INSERT INTO t VALUES (?, ?)", [(1, lo), (2, hi), (3, None)]
        )
        conn.commit()
    finally:
        conn.close()

    records = list(SqliteReader().iter_records(db, "t"))
    assert len(records) == 3  # no row dropped to a float collision
    assert [(r.fields["id"], r.fields["big"]) for r in records] == [
        (1, lo),
        (2, hi),
        (3, None),  # NULL stays None, never NaN
    ]
    assert all(isinstance(r.fields["big"], int) for r in records if r.fields["big"] is not None)


def test_fingerprint_matches_int_against_promoted_float() -> None:
    # The same value arriving as int in one pass and float64 in the other (because that
    # pass carried a NULL) must still match as the SAME committed row, not double-count.
    cols = ["n"]
    df_db = pd.DataFrame([{"n": 5}], columns=cols, dtype=_OBJ)  # exact int
    df_side = pd.DataFrame([{"n": 5.0}, {"n": None}], columns=cols)  # float64 + NaN
    merged = merge_with_source(df_db, df_side, sidecar_label="wal")
    labels = list(merged[SOURCE_COLNAME])
    assert labels.count("both") == 1  # 5 == 5.0 matched cross-pass
    assert labels.count("wal") == 1   # only the NULL row is genuinely sidecar-only
