"""SQLite format reader: stage the db + siblings, run the unchanged two-pass merge.

The WAL/journal two-pass merge that determines the ROW SET is unchanged (it lives in
``model_atlas/sqlite``); this reader only relabels the merge origin into the row-aligned
``recovery_state`` enrichment and keeps it out of ``source_fields``/the dataframe.

``read`` always stages a fresh throwaway group (db + WAL/SHM/journal) and keeps the full
before/after integrity chain. The metadata-only probes (``peek_fields``/``list_subtables``),
which the matcher calls once per (preset, selector) candidate, take a cheaper path: an on-disk
source is opened in place read-only immutable (no copy, no hash), and a zip entry is staged
ONCE per physical source and reused across every later probe (mirroring ``XmlReader``'s
per-report cache). The staged probe copies are cleaned up at interpreter exit.
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import shutil
import sqlite3
from typing import Any

from model_atlas.sources.container import SourceFile, staged_group
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
from model_atlas.sqlite._sql import quote_identifier
from model_atlas.sources.staging import STAGING_ALWAYS, StagedGroup

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

    def __init__(self) -> None:
        # Zip-entry probe cache: a physical zip-contained db is staged ONCE and reused across
        # every peek/list probe (keyed like XmlReader._cache_key). On-disk sources bypass this
        # entirely (opened in place). Cleaned up at exit; a single reader instance lives for
        # the whole run (register_reader instantiates once).
        self._probe_cache: dict[str, StagedGroup] = {}
        atexit.register(self._cleanup)

    def read(self, file: SourceFile, params: dict) -> ReadResult:
        table = params.get("table")
        sql = params.get("sql")
        if bool(table) == bool(sql):
            raise ValueError("a sqlite selector must define exactly one of table or sql")

        container = file.container
        with staged_group(container, file) as staged:
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
            source_fields = tuple(str(c) for c in with_sidecars.columns)
            merged = merge_with_source(
                db_only, with_sidecars, sidecar_label=_sidecar_label(staged.members)
            )

        recovery = tuple(
            _LABEL_TO_RECOVERY.get(label, RECOVERY_LIVE) for label in merged[SOURCE_COLNAME]
        )
        dataframe = merged.drop(columns=[SOURCE_COLNAME, SOURCE_ROW_NUMBER_COLNAME])
        return ReadResult(
            dataframe=dataframe,
            source_fields=source_fields,
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

    def peek_fields(self, file: SourceFile, selector: Any = None) -> set[str] | None:
        """Columns of the selector's table, or None when they cannot be verified.

        A metadata-only probe: it opens the db read-only immutable and reads only the schema
        (``PRAGMA table_info``). Immutable mode ignores any WAL/journal content, so a column
        added only in an un-checkpointed WAL would not be seen — acceptable for a probe, whose
        job is a best-effort applicability check, not the authoritative row read.
        """
        table = getattr(selector, "table", None) if selector is not None else None
        if not table:
            return None
        # Staging (for a zip entry) happens OUTSIDE the try, so a staging failure still
        # propagates exactly as before; the try wraps only the schema read.
        db_path = self._probe_db_path(file)
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
            ) as conn:
                # Route the preset-supplied table name through the single validated quoting
                # path: quote_identifier raises on embedded quotes/NUL/control chars, which the
                # except below turns into a None ("cannot verify") result rather than executing.
                cur = conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
                return {str(row[1]) for row in cur.fetchall()}
        except Exception:  # noqa: BLE001
            log.debug("sqlite peek_fields failed for %s", file.logical_path, exc_info=True)
            return None

    def list_subtables(self, file: SourceFile) -> list[str]:
        db_path = self._probe_db_path(file)
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
            ) as conn:
                cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                return [str(row[0]) for row in cur.fetchall()]
        except Exception:  # noqa: BLE001
            log.debug(f"sqlite list_subtables failed for {file.logical_path}", exc_info=True)
            return []

    # --- probe internals ----------------------------------------------------

    def _probe_db_path(self, file: SourceFile):
        """Path to a sqlite db to open READ-ONLY for a schema probe (no full staging chain).

        On-disk source (loose file / folder): the ORIGINAL path, opened later with
        ``mode=ro&immutable=1`` — no copy, no hash. This does NOT violate the never-open-the-
        original rule for reads: ``read()`` still stages a throwaway copy and keeps the full
        integrity chain; probes are read-only metadata queries and immutable mode guarantees
        SQLite never writes (it never touches the WAL/journal and takes no locks), so the
        evidence is provably unmodified.

        Zip entry (no on-disk path): stage the db group ONCE per physical source and cache it,
        reusing the staged copy across every later probe (mirrors ``XmlReader``'s per-report
        cache) so one archived db is not copied+hashed once per candidate."""
        disk = file.container.ondisk_path(file)
        if disk is not None:
            return disk
        key = self._cache_key(file)
        cached = self._probe_cache.get(key)
        if cached is None:
            # finalize is intentionally skipped: a cached probe makes no integrity claim (that
            # is read()'s job). The copy is torn down at interpreter exit via _cleanup.
            cached = file.container.stage_group(file)
            self._probe_cache[key] = cached
        return cached.members["db"]

    @staticmethod
    def _cache_key(file: SourceFile) -> str:
        # Key on the physical source so two same-named dbs (e.g. the same filename in different
        # archives/folders) never share a cache entry. A zip entry has no ondisk_path, so fall
        # back to the archive path + inner logical path (mirrors XmlReader._cache_key).
        disk = file.container.ondisk_path(file)
        if disk is not None:
            return str(disk)
        archive = getattr(file.container, "path", file.container.root_label)
        return f"{archive}::{file.logical_path}"

    def _cleanup(self) -> None:
        for staged in self._probe_cache.values():
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)
        self._probe_cache.clear()
