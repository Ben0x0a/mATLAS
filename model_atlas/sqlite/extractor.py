"""Extract a single table from a SQLite database, in two flavors:

- "db-only"  : the database file is opened without its sidecar siblings,
               so the result reflects only data committed to the main file.
- "db+sidecars" : the database is opened alongside its WAL/SHM or rollback
                  journal sidecars, so SQLite applies the sidecar state at
                  read time.

The two views are then diffed downstream (see dedup.py) to label rows by
origin (db vs sidecar-visible view).
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

from model_atlas.sqlite._sql import quote_identifier as _quote_identifier
from model_atlas.sqlite.sql_query import harden_connection


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    )
    return cur.fetchone() is not None


def _connect(db_path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """Open ``db_path`` read-only.

    Always read-only — forensic working copies must never be mutated by
    SQLite, even if a journal/WAL sibling sits next to the file. When
    ``immutable=True``, SQLite is additionally told to skip journal/WAL
    recovery entirely; use this for the "db-only" extraction pass where we
    have intentionally not copied any sidecar.
    """
    if immutable:
        uri = f"file:{db_path}?mode=ro&immutable=1"
    else:
        uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _read_table(db_path: Path, table: str, *, immutable: bool = False) -> pd.DataFrame:
    quoted = _quote_identifier(table)
    # contextlib.closing (not a bare `with`): sqlite3's own context manager only
    # commits/rolls back the transaction, it never closes the connection. A leaked
    # handle keeps the (temp) db file locked on Windows, which blocks TemporaryDirectory
    # cleanup and any later reopen ("file used by someone else"). Close it explicitly.
    with contextlib.closing(_connect(db_path, immutable=immutable)) as conn:
        if not _table_exists(conn, table):
            raise ValueError(f"Table {table!r} not found in {db_path}")
        df = pd.read_sql_query(f"SELECT * FROM {quoted}", conn)
    return df


def _read_query(db_path: Path, sql: str, *, immutable: bool = False) -> pd.DataFrame:
    """Run a validated custom query read-only and return the result.

    The connection is hardened (read-only authorizer + table-qualified column
    names) before the query runs, so a query that somehow slipped past the
    syntactic gate still cannot mutate the working copy. Join columns come back
    as ``table.column`` (see ``model_atlas.sqlite.sql_query.harden_connection``)."""
    # See _read_table: close the connection explicitly so the temp db is released
    # (a bare sqlite3 `with` does not close it — a Windows file-lock hazard).
    with contextlib.closing(_connect(db_path, immutable=immutable)) as conn:
        harden_connection(conn)
        df = pd.read_sql_query(sql, conn)
    return df


@contextlib.contextmanager
def _prepared_copy(
    db_path: Path,
    include_sidecars: bool,
    wal_path: Optional[Path],
    shm_path: Optional[Path],
    journal_path: Optional[Path],
) -> Iterator[Path]:
    """Copy ``db_path`` (and, when requested, its sidecars) into a throwaway
    temp dir and yield the local copy's path.

    Copying first guarantees SQLite never opens — let alone mutates — the
    original evidence file. When ``include_sidecars`` is False, only the main
    db is copied, so any WAL/journal sibling next to the original is ignored;
    when True, the provided WAL/SHM/rollback-journal sidecars are copied so
    SQLite can apply their state at read time."""
    db_path = Path(db_path)
    with tempfile.TemporaryDirectory() as td:
        local_db = Path(td) / db_path.name
        shutil.copy2(db_path, local_db)
        if include_sidecars:
            if wal_path is not None and Path(wal_path).exists():
                shutil.copy2(wal_path, local_db.with_name(db_path.name + "-wal"))
            if shm_path is not None and Path(shm_path).exists():
                shutil.copy2(shm_path, local_db.with_name(db_path.name + "-shm"))
            if journal_path is not None and Path(journal_path).exists():
                shutil.copy2(journal_path, local_db.with_name(db_path.name + "-journal"))
        yield local_db


def extract_table(
    db_path: Path,
    table: str,
    include_sidecars: bool,
    wal_path: Optional[Path] = None,
    shm_path: Optional[Path] = None,
    journal_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Return the contents of `table` as a DataFrame.

    When include_sidecars is False, the database file is copied alone into a fresh
    temp dir before being opened, so any WAL sibling that may sit next to
    db_path on disk is ignored. When include_sidecars is True, WAL/SHM and rollback
    journal sidecars are copied if provided.
    """
    with _prepared_copy(db_path, include_sidecars, wal_path, shm_path, journal_path) as local_db:
        # db-only pass: immutable=1 so SQLite ignores any stray sidecar.
        # db+sidecar pass: plain read-only — SQLite applies WAL transparently.
        # Rollback journals require write mode to be applied; we accept the
        # limitation (rollback recovery not performed) rather than mutate the
        # working copy. See plan P1.4 for the longer-term plan to handle that
        # in a separate throwaway pass.
        return _read_table(local_db, table, immutable=not include_sidecars)


def extract_query(
    db_path: Path,
    sql: str,
    include_sidecars: bool,
    wal_path: Optional[Path] = None,
    shm_path: Optional[Path] = None,
    journal_path: Optional[Path] = None,
) -> pd.DataFrame:
    """Run a validated custom ``SELECT`` against a copy of the database.

    Same two-pass contract as :func:`extract_table` — the only difference is
    that the caller's query replaces ``SELECT * FROM <table>``. Running the
    identical query against the db-only and db+sidecars copies lets the
    downstream merge label which rows are visible only via the WAL/journal,
    exactly as for a single-table extraction (so joins inherit the
    WAL-visibility guarantee). The query is expected to have passed
    ``model_atlas.sqlite.sql_query.validate_custom_sql``; read-only execution is enforced
    regardless by the hardened connection.
    """
    with _prepared_copy(db_path, include_sidecars, wal_path, shm_path, journal_path) as local_db:
        return _read_query(local_db, sql, immutable=not include_sidecars)
