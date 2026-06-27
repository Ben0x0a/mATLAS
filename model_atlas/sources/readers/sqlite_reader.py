"""SQLite format reader: stage the db + siblings, run the unchanged two-pass merge.

The WAL/journal two-pass merge that determines the ROW SET is unchanged (it lives in
``model_atlas/sqlite``); this reader only relabels the merge origin into the row-aligned
``recovery_state`` enrichment and keeps it out of ``source_columns``/the dataframe.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
from typing import Any

from model_atlas.sources.container import SourceFile
from model_atlas.sources.readers.base import (
    RECOVERY_JOURNAL,
    RECOVERY_LIVE,
    RECOVERY_WAL,
    ReadResult,
    register_reader,
)
from model_atlas.sqlite.dedup import (
    SOURCE_COLNAME,
    SOURCE_ROW_NUMBER_COLNAME,
    merge_with_source,
)
from model_atlas.sqlite.extractor import extract_query, extract_table
from model_atlas.sqlite.sql_query import validate_custom_sql
from model_atlas.sources.staging import STAGING_ALWAYS

log = logging.getLogger(__name__)

# Map the merge origin label onto the cross-format recovery vocabulary.
_LABEL_TO_RECOVERY = {
    "both": RECOVERY_LIVE,
    "db_only_unique": RECOVERY_LIVE,
    "wal": RECOVERY_WAL,
    "journal": RECOVERY_JOURNAL,
}


def _sidecar_label(members: dict) -> str:
    if "wal" in members:
        return RECOVERY_WAL
    if "journal" in members:
        return RECOVERY_JOURNAL
    return "sidecar"


@register_reader
class SqliteReader:
    format = "sqlite"
    # Always copy: the WAL/journal two-pass merge needs a stable staged group (db + WAL/SHM/
    # journal siblings) on disk, which cannot be done by reading the original in place.
    staging_mode = STAGING_ALWAYS

    def read(self, file: SourceFile, params: dict) -> ReadResult:
        table = params.get("table")
        sql = params.get("sql")
        if bool(table) == bool(sql):
            raise ValueError("a sqlite selector must define exactly one of table or sql")

        container = file.container
        staged = container.stage_group(file)
        try:
            db_path = staged.members["db"]
            wal = staged.members.get("wal")
            shm = staged.members.get("shm")
            journal = staged.members.get("journal")
            if sql:
                validated = validate_custom_sql(str(sql))
                db_only = extract_query(db_path, validated, include_sidecars=False)
                with_sidecars = extract_query(
                    db_path, validated, include_sidecars=True,
                    wal_path=wal, shm_path=shm, journal_path=journal,
                )
                subunit = "query"
            else:
                table = str(table)
                db_only = extract_table(db_path, table, include_sidecars=False)
                with_sidecars = extract_table(
                    db_path, table, include_sidecars=True,
                    wal_path=wal, shm_path=shm, journal_path=journal,
                )
                subunit = table
            source_columns = tuple(str(c) for c in with_sidecars.columns)
            merged = merge_with_source(
                db_only, with_sidecars, sidecar_label=_sidecar_label(staged.members)
            )
            container.finalize(staged)
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)

        recovery = tuple(
            _LABEL_TO_RECOVERY.get(label, RECOVERY_LIVE) for label in merged[SOURCE_COLNAME]
        )
        dataframe = merged.drop(columns=[SOURCE_COLNAME, SOURCE_ROW_NUMBER_COLNAME])
        return ReadResult(
            dataframe=dataframe,
            source_columns=source_columns,
            recovery_state=recovery,
            metadata={
                "format": "sqlite",
                "subunit": subunit,
                "table": table if not sql else None,
                "custom_sql": str(sql) if sql else None,
                "source_fingerprint": staged.fingerprint,
                "row_count_db_only": len(db_only),
                "row_count_with_sidecars": len(with_sidecars),
                "row_count_merged": len(merged),
                "integrity": staged.integrity,
            },
        )

    def peek_columns(self, file: SourceFile, selector: Any = None) -> set[str] | None:
        table = getattr(selector, "table", None) if selector is not None else None
        if not table:
            return None
        container = file.container
        staged = container.stage_group(file)
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{staged.members['db']}?mode=ro&immutable=1", uri=True)
            ) as conn:
                cur = conn.execute(f'PRAGMA table_info("{table}")')
                return {str(row[1]) for row in cur.fetchall()}
        except Exception:  # noqa: BLE001
            log.debug(f"sqlite peek_columns failed for {file.logical_path}", exc_info=True)
            return None
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)

    def list_subtables(self, file: SourceFile) -> list[str]:
        container = file.container
        staged = container.stage_group(file)
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{staged.members['db']}?mode=ro&immutable=1", uri=True)
            ) as conn:
                cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                return [str(row[0]) for row in cur.fetchall()]
        except Exception:  # noqa: BLE001
            log.debug(f"sqlite list_subtables failed for {file.logical_path}", exc_info=True)
            return []
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)
