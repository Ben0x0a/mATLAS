"""SQLite format reader (ingest brick): R9 rowid/PK order, WAL/journal recovery merge.

Defines:    ``SqliteReader`` (format "sqlite").
Used by:    the ``format -> Reader`` registry (registered on import).
Depends on: ``model_atlas.core.determinism`` (``sqlite_enumeration_sql``, ``numbered_
            records`` — rule 1; ``blob_envelope`` — rule 7), ``model_atlas.core.
            vocabularies`` (``RecordState``), ``model_atlas.ingest.readers.base``/
            ``registry``, ``model_atlas.sqlite._sql.quote_identifier`` (the T90 fix),
            ``model_atlas.sqlite.sql_query`` (``validate_custom_sql``/``harden_connection``
            for the custom-SQL path), ``model_atlas.sqlite.dedup.merge_with_source`` (the
            unchanged two-pass origin-labelling algorithm), and ``pandas`` (the shape
            ``merge_with_source`` operates on).

Ported from ``sources/readers/sqlite_reader.py``: the WAL/journal two-pass
merge algorithm (``merge_with_source``) is reused UNCHANGED. What differs is the row
FETCH: the old reader (via ``model_atlas.sqlite.extractor.extract_table``) ran a bare
``SELECT * FROM table`` with no explicit order, relying on SQLite's incidental scan order.
R9 rule 1 requires a PINNED order (``rowid``, or primary-key order for a ``WITHOUT ROWID``
table), so this reader builds its own two-pass fetch using ``sqlite_enumeration_sql`` for
the ``SELECT`` text instead of calling ``extract_table``/``extract_query`` — deliberately
NOT reusing those two helpers, since neither can be given a rule-1-correct query. The
staging-copy step they perform (main db + optional WAL/SHM/journal siblings, copied into a
throwaway temp dir so SQLite never opens the original) is duplicated here for the same
reason, mirroring their private ``_prepared_copy`` shape rather than importing it.

Carries the T90 security fix: ``peek_fields`` quotes the selector-supplied table name via
``quote_identifier`` — never an f-string into SQL — so a table name containing ``"`` makes
``quote_identifier`` raise, which is caught and turned into ``None`` (cannot verify),
never executed.

Sibling discovery: since T21 (discover/containers) has not landed, ``source`` is a plain
path already resolved to a real file on disk; WAL/SHM/journal siblings are found next to it
by the standard SQLite naming convention (``<path>-wal`` etc.), exactly what the old
reader's container-staged group already exposed.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd

from model_atlas.core.determinism import blob_envelope, numbered_records, sqlite_enumeration_sql
from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader
from model_atlas.sqlite._sql import quote_identifier
from model_atlas.sqlite.dedup import SOURCE_COLNAME, SOURCE_ROW_NUMBER_COLNAME, merge_with_source
from model_atlas.sqlite.sql_query import harden_connection, validate_custom_sql

log = logging.getLogger(__name__)

_SIBLING_SUFFIXES = ("-wal", "-shm", "-journal")


def _siblings(db_path: Path) -> dict[str, Path]:
    """The WAL/SHM/journal paths that actually exist next to ``db_path``."""
    found = {}
    for key, suffix in zip(("wal", "shm", "journal"), _SIBLING_SUFFIXES):
        candidate = db_path.with_name(db_path.name + suffix)
        if candidate.exists():
            found[key] = candidate
    return found


def _sidecar_label(siblings: dict[str, Path]) -> str:
    if "wal" in siblings:
        return "wal"
    if "journal" in siblings:
        return "journal"
    return "sidecar"


@contextlib.contextmanager
def _prepared_copy(
    db_path: Path, siblings: dict[str, Path], *, include_sidecars: bool,
) -> Iterator[Path]:
    """Copy ``db_path`` (and, when requested, its siblings) into a throwaway temp dir.

    WHY copy first: SQLite must never open — let alone let a WAL/journal replay mutate —
    the original evidence file.
    """
    with tempfile.TemporaryDirectory(prefix="matlas-sqlite-") as tmp_dir:
        local_db = Path(tmp_dir) / db_path.name
        shutil.copy2(db_path, local_db)
        if include_sidecars:
            for key, suffix in zip(("wal", "shm", "journal"), _SIBLING_SUFFIXES):
                sibling = siblings.get(key)
                if sibling is not None:
                    shutil.copy2(sibling, local_db.with_name(local_db.name + suffix))
        yield local_db


def _connect(db_path: Path, *, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro&immutable=1" if immutable else f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _fetch(conn: sqlite3.Connection, sql: str) -> tuple[list[dict[str, Any]], list[str]]:
    # Bytes are kept raw here (never the rule-7 envelope yet) because merge_with_source
    # fingerprints each row through a hashable tuple of its values next — bytes hash fine,
    # a dict does not. The envelope is applied once, after the merge, in iter_records.
    cursor = conn.execute(sql)
    columns = [description[0] for description in cursor.description]
    rows = [dict(zip(columns, record)) for record in cursor.fetchall()]
    return rows, columns


def _read_pass(
    db_path: Path, *, table: str | None, sql: str | None, immutable: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    with contextlib.closing(_connect(db_path, immutable=immutable)) as conn:
        if sql is not None:
            # No rule-1 ORDER BY here: an arbitrary join/aggregate query has no single
            # rowid/PK to order by. This mirrors the old reader's extract_query, which was
            # already unordered — not a regression this task introduces.
            harden_connection(conn)
            return _fetch(conn, sql)
        # rule 1: rowid order (WITHOUT ROWID: primary-key order) — the pinned SELECT text.
        return _fetch(conn, sqlite_enumeration_sql(conn, str(table)))


@register_reader
class SqliteReader:
    format = "sqlite"

    def subunits(self, source: Path) -> list[str]:
        try:
            with contextlib.closing(_connect(source, immutable=True)) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                return [str(row[0]) for row in cursor.fetchall()]
        except Exception:  # noqa: BLE001 - a probe failure must never abort matching
            log.debug(f"sqlite subunits failed for {source}", exc_info=True)
            return []

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]:
        params = params or {}
        sql = params.get("sql")
        table = subunit
        if bool(table) == bool(sql):
            raise ValueError(
                "a sqlite read needs exactly one of a table (subunit) or params['sql']"
            )
        validated_sql = validate_custom_sql(str(sql)) if sql else None
        siblings = _siblings(source)
        sidecar_label = _sidecar_label(siblings)

        with _prepared_copy(source, siblings, include_sidecars=False) as db_only_copy:
            db_only_rows, db_only_columns = _read_pass(
                db_only_copy, table=table, sql=validated_sql, immutable=True,
            )
        with _prepared_copy(source, siblings, include_sidecars=True) as full_copy:
            with_sidecars_rows, with_sidecars_columns = _read_pass(
                full_copy, table=table, sql=validated_sql, immutable=False,
            )
        if db_only_columns != with_sidecars_columns:
            raise ValueError(
                f"column mismatch between the db-only and db+sidecars reads of {source}: "
                f"{db_only_columns} vs {with_sidecars_columns}"
            )
        columns = db_only_columns

        merged = merge_with_source(
            pd.DataFrame(db_only_rows, columns=columns),
            pd.DataFrame(with_sidecars_rows, columns=columns),
            sidecar_label=sidecar_label,
        )

        # Split back into live vs recovered, preserving each half's own order, then hand
        # BOTH to numbered_records — rule 1 owns "live first, recovered after, one
        # continuing sequence", never re-derived here.
        live: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []
        for record in merged.to_dict(orient="records"):
            origin = record.pop(SOURCE_COLNAME)
            record.pop(SOURCE_ROW_NUMBER_COLNAME)
            # merge_with_source labels every row 'both' / 'db_only_unique' (live) or
            # sidecar_label (recovered) — never any other value.
            (recovered if origin == sidecar_label else live).append(record)

        for line_no, fields, is_recovered in numbered_records(live, recovered):
            # rule 7: a binary cell becomes the {"$blob": {...}} envelope, applied only now
            # (after the fingerprint-based merge, which needs bytes' native hashability).
            fields = {
                column: (blob_envelope(value) if isinstance(value, bytes) else value)
                for column, value in fields.items()
            }
            yield RawRecord(
                line_no=line_no,
                fields=fields,
                recovery_state=RecordState.RECOVERED if is_recovered else RecordState.LIVE,
            )

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        """Columns of ``subunit`` (a table name), or None when they cannot be verified.

        T90 carry-over: ``quote_identifier`` is the ONLY path a table name reaches SQL
        through. It raises on an embedded quote/NUL/control char, which the except below
        turns into None ("cannot verify") — the identifier is never f-string-interpolated
        and PRAGMA is never executed with an unvalidated name.
        """
        if not subunit:
            return None
        try:
            with contextlib.closing(_connect(source, immutable=True)) as conn:
                cursor = conn.execute(f"PRAGMA table_info({quote_identifier(subunit)})")
                return {str(row[1]) for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001
            log.debug(f"sqlite peek_fields failed for {source}", exc_info=True)
            return None
