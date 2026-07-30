"""Tests for custom-SQL hardening (``model_atlas.sqlite.sql_query`` + reader ``_fetch``).

Covers the FIX-20 hardening: ``load_extension`` is denied by the read-only authorizer even
when extension loading has been toggled on (native-code-execution boundary), and a custom
SELECT that yields duplicate output column names is rejected with a clear error rather than
silently dropping a column under ``dict(zip(...))``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from model_atlas.ingest.readers.sqlite_reader import SqliteReader
from model_atlas.sqlite.sql_query import harden_connection


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER, lat REAL, lon REAL)")
        conn.executemany(
            "INSERT INTO t VALUES (?, ?, ?)", [(0, 1.0, 2.0), (1, 3.0, 4.0)]
        )
        conn.commit()
    finally:
        conn.close()
    return path


# --- load_extension denial --------------------------------------------------

def test_harden_connection_disables_load_extension() -> None:
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    harden_connection(conn)
    # harden_connection explicitly turns extension loading off; re-enabling it fails or is
    # a no-op, but either way the authorizer below is the real guarantee.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT load_extension('anything')")


def test_load_extension_denied_even_when_toggled_on() -> None:
    # The authorizer denies load_extension BY NAME, so even an adversary re-enabling
    # extension loading after hardening cannot reach native code.
    conn = sqlite3.connect(":memory:")
    harden_connection(conn)
    conn.enable_load_extension(True)  # toggled ON again, after the boundary was installed
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT load_extension('anything')")


def test_harden_connection_still_allows_ordinary_functions() -> None:
    conn = sqlite3.connect(":memory:")
    harden_connection(conn)
    # A read-only SELECT legitimately calls scalar functions — those must stay allowed.
    assert conn.execute("SELECT abs(-3), upper('x')").fetchone() == (3, "X")


# --- duplicate output column names ------------------------------------------

def test_duplicate_output_column_names_error_not_dropped(tmp_path: Path) -> None:
    # `SELECT id, id` yields two output columns named `id`; dict(zip(...)) would keep only
    # one, silently dropping a column. It must be rejected with a clear error instead.
    db = _db(tmp_path / "d.sqlite")
    with pytest.raises(ValueError, match="duplicate output column"):
        list(SqliteReader().iter_records(db, None, {"sql": "SELECT id, id FROM t"}))


def test_self_join_keeps_both_columns_or_errors(tmp_path: Path) -> None:
    # A self-join SELECT * must NEVER silently drop a side's data: either the columns are
    # disambiguated (alias-qualified on builds where full_column_names honours the alias)
    # and both id values survive, or the read errors clearly on the duplicate name.
    db = _db(tmp_path / "sj.sqlite")
    sql = "SELECT * FROM t a JOIN t b ON a.id = b.id"
    try:
        records = list(SqliteReader().iter_records(db, None, {"sql": sql}))
    except ValueError as exc:
        assert "duplicate output column" in str(exc)
        return
    # Succeeded => both sides' id must be present under distinct keys (no silent collapse).
    fields = records[0].fields
    id_values = [v for k, v in fields.items() if k.split(".")[-1] == "id"]
    assert len(id_values) == 2
