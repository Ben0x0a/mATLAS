"""Tests for the sqlite_db export brick (model_atlas/export/sqlite_db.py).

Persists a small store (the manual store — exercises every layer L0-L2, unlike the
sqlite3-default-empty case) and reopens the file, checking three requirements: it
reopens; it carries ``PRAGMA user_version`` + the ``matlas_meta`` row
+ the ``application_id`` self-ingestion stamp; and ``PRAGMA foreign_key_check`` is
clean.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from model_atlas.core.schema import APPLICATION_ID, FAMILIES_VERSION, SCHEMA_VERSION, USER_VERSION
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.sqlite_db import SqliteDbExporter

from tests.export._pipeline import build_manual_store


def test_sqlite_db_filename_and_persistence(tmp_path: Path) -> None:
    with RunStore() as store:
        build_manual_store(store)
        written = SqliteDbExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="my_run"))

    assert written == [tmp_path / "my_run.matlas.sqlite"]
    assert written[0].exists()


def test_sqlite_db_reopens_with_version_stamps_and_clean_fk_check(tmp_path: Path) -> None:
    with RunStore() as store:
        build_manual_store(store)
        written = SqliteDbExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="run1"))

    connection = sqlite3.connect(written[0])
    try:
        # PRAGMA user_version — the schema-generation gate a tool can check without
        # opening a table.
        assert connection.execute("PRAGMA user_version").fetchone()[0] == USER_VERSION

        # application_id — the R16 self-ingestion stamp (discovery refuses/flags a
        # mATLAS run DB fed back in as evidence).
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID

        # matlas_meta — one row, carrying the schema/families versions.
        meta_rows = connection.execute(
            "SELECT schema_version, families_version FROM matlas_meta").fetchall()
        assert meta_rows == [(SCHEMA_VERSION, FAMILIES_VERSION)]

        # A clean run DB: no dangling foreign key.
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        # The whole relational model persisted, not a flat-table dump: spot-check a
        # couple of tables the old ``sqlite`` exporter never wrote directly.
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"assertion", "entity", "spatial_integration", "timespan_integration"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM assertion").fetchone()[0] == 2
    finally:
        connection.close()


def test_sqlite_db_writes_one_file_for_the_whole_run(tmp_path: Path) -> None:
    """One file per format: the whole run's data lives in the single persisted database."""
    with RunStore() as store:
        build_manual_store(store)
        written = SqliteDbExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="run1"))
    assert written == [tmp_path / "run1.matlas.sqlite"]
