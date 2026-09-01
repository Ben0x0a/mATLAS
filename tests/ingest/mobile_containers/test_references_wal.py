"""Why ``references.sqlite3`` must be opened ``mode=ro`` and never ``immutable=1``.

Defines:    a demonstration that an ``immutable=1`` read misses rows that live only in the
            write-ahead log, and a check that ``read_references`` does not miss them.
Used by:    the test suite.
Depends on: the standard library and the package's ``ios.references`` reader.

This test exists to RECORD A REASON, not only to catch a regression. ``immutable=1`` is the
natural reflex on read-only evidence — it tells SQLite the file cannot change, skips all
locking, and is faster. It is also silently wrong here: it makes SQLite ignore the
write-ahead log entirely. On the verified acquisition the log held 11 of 1088
``code_signing_info`` rows and 4 of 317 ``child_bundles`` rows, and those rows are the most
recently installed apps — the ones an examiner is most likely to be asked about. Nothing
errors; the answer is just quietly incomplete. The fixture below reproduces that exact
situation at four rows, so anyone tempted to "optimise" the open flag meets the reason first.
"""
from __future__ import annotations

import shutil
import sqlite3
import zipfile
from pathlib import Path

from model_atlas.ingest.utils.mobile_containers import ZipOpener
from model_atlas.ingest.utils.mobile_containers.ios.references import read_references
from tests.ingest.mobile_containers.conftest import REFERENCES_PATH

_COMMITTED_IDS = ("com.example.old.one", "com.example.old.two")
_WAL_ONLY_IDS = ("com.example.recent.one", "com.example.recent.two")


def _build_database_with_pending_wal(directory: Path) -> Path:
    """A database whose two most recent rows exist only in the write-ahead log.

    HOW: write the schema and the older rows in rollback-journal mode so they land in the
    main file, then switch to WAL with checkpointing disabled and add the recent rows, then
    copy the files out while the connection is still open — closing it would checkpoint the
    log into the main file and destroy the very situation being reproduced.
    """
    live = directory / "live.sqlite3"
    connection = sqlite3.connect(live)
    with connection:
        connection.execute(
            "CREATE TABLE code_signing_info(id INTEGER PRIMARY KEY,"
            "code_signing_id_text TEXT NOT NULL UNIQUE,data_container_class INTEGER NOT NULL)"
        )
        connection.execute("CREATE TABLE code_signing_data(cs_info_id INTEGER, data BLOB)")
        connection.execute(
            "CREATE TABLE child_bundles(child_code_signing_id_text TEXT, parent_id INTEGER)"
        )
        for row_id, identifier in enumerate(_COMMITTED_IDS, start=1):
            connection.execute(
                "INSERT INTO code_signing_info VALUES (?,?,2)", (row_id, identifier)
            )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    with connection:
        for row_id, identifier in enumerate(_WAL_ONLY_IDS, start=len(_COMMITTED_IDS) + 1):
            connection.execute(
                "INSERT INTO code_signing_info VALUES (?,?,2)", (row_id, identifier)
            )

    staged = directory / "references.sqlite3"
    shutil.copy(live, staged)
    shutil.copy(live.with_name(live.name + "-wal"), staged.with_name(staged.name + "-wal"))
    connection.close()
    return staged


def _identifiers(database: Path, query_parameters: str) -> set[str]:
    connection = sqlite3.connect(f"file:{database}?{query_parameters}", uri=True)
    try:
        return {row[0] for row in connection.execute(
            "SELECT code_signing_id_text FROM code_signing_info"
        )}
    finally:
        connection.close()


def test_immutable_read_would_miss_the_write_ahead_log(tmp_path: Path):
    """The recorded reason: ``immutable=1`` silently returns a stale view of the device."""
    database = _build_database_with_pending_wal(tmp_path)

    assert _identifiers(database, "immutable=1") == set(_COMMITTED_IDS)
    assert _identifiers(database, "mode=ro") == set(_COMMITTED_IDS) | set(_WAL_ONLY_IDS)


def test_read_references_stages_the_sidecar_and_sees_the_recent_rows(tmp_path: Path):
    """The reader must reach the WAL rows through an acquisition, not just on disk."""
    database = _build_database_with_pending_wal(tmp_path)
    archive = tmp_path / "acquisition.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(database, REFERENCES_PATH)
        zf.write(database.with_name(database.name + "-wal"), REFERENCES_PATH + "-wal")

    with ZipOpener(archive) as opener:
        index = read_references(opener)

    assert {record.identifier for record in index.records} == (
        set(_COMMITTED_IDS) | set(_WAL_ONLY_IDS)
    )
    assert index.findings == ()
