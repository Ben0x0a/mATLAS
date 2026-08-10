"""SQLite format reader (ingest brick): normative rowid/PK order, WAL/journal recovery merge.

Defines:    ``SqliteReader`` (format "sqlite").
Used by:    the ``format -> Reader`` registry (registered on import).
Depends on: ``model_atlas.core.determinism`` (``sqlite_enumeration_sql``, ``numbered_
            records`` — rule 1; ``blob_envelope`` — rule 7), ``model_atlas.core.
            vocabularies`` (``RecordState``), ``model_atlas.ingest.readers.base``/
            ``registry``, ``model_atlas.sqlite._sql.quote_identifier`` (the security fix
            below), ``model_atlas.sqlite.sql_query`` (``validate_custom_sql``/``harden_connection``
            for the custom-SQL path), ``model_atlas.sqlite.dedup.merge_with_source`` (the
            multiset two-pass origin-labelling algorithm), ``model_atlas.sqlite.carve``
            (freelist + free-space carving of deleted rows), and ``pandas`` (the shape
            ``merge_with_source`` operates on).

Recovery has THREE sources here, all funnelled into one ``recovered`` list numbered after
the live rows (rule 1's ``numbered_records``): WAL and journal sidecars (the two-pass
merge), plus rows CARVED from the main file's own unallocated space
(freelist pages + leaf-page free space), which is where deleted rows survive once a WAL
has been checkpointed away. Carving is ON BY DEFAULT (params['carve']=False opts out):
silently dropping recoverable evidence is the failure mode to avoid. It runs only on the
table path (a custom SQL join has no single table/schema to carve) and reads the file's
RAW bytes directly — never the staging copy or SQLite's row API, which only see live rows.

Ported from ``sources/readers/sqlite_reader.py``: the WAL/journal two-pass
merge (``merge_with_source``) keeps the same two-pass SHAPE but now matches the passes by
VALUE MULTISET, so two genuinely-distinct rows with identical column values BOTH survive
(observation semantics) while only the true cross-pass overlap is coalesced — see
``model_atlas.sqlite.dedup``. What else differs from the old engine is the row
FETCH: the old engine's reader ran a bare ``SELECT * FROM table`` with no explicit order,
relying on SQLite's incidental scan order. Rule 1 requires a PINNED order (``rowid``, or
primary-key order for a ``WITHOUT ROWID`` table), so this reader builds its own two-pass
fetch using ``sqlite_enumeration_sql`` for the ``SELECT`` text — a rule-1-correct query the
old bare select could not give. The staging-copy step (main db + optional WAL/SHM/journal
siblings, copied into a throwaway temp dir so SQLite never opens the original) is done here
directly for the same reason.

Carries a security fix: ``peek_fields`` quotes the selector-supplied table name via
``quote_identifier`` — never an f-string into SQL — so a table name containing ``"`` makes
``quote_identifier`` raise, which is caught and turned into ``None`` (cannot verify),
never executed.

Sibling discovery: since discover/containers has not landed, ``source`` is a plain
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
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd

from model_atlas.core.determinism import blob_envelope, numbered_records, sqlite_enumeration_sql
from model_atlas.core.vocabularies import RecordState, RecoverySource
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader
from model_atlas.sqlite._sql import quote_identifier
from model_atlas.sqlite.carve import CARVE_SOURCE_COLNAME, carve_table
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


# A private key carried on each record dict from the split loop through numbered_records
# (which passes records through untouched) to the yield, where it is popped and becomes the
# row's ``recovery_source``. Private (``_meta_``) so it can never collide with a real column.
_RECOVERY_SOURCE_KEY = "_meta_recovery_source"


def _sidecar_recovery_source(sidecar_label: str) -> RecoverySource:
    """The ``RecoverySource`` for a sidecar-recovered row: WAL or JOURNAL by the label,
    UNKNOWN for the unnamed-sidecar fallback (a recovered row of indeterminate origin)."""
    if sidecar_label == RecoverySource.WAL.value:
        return RecoverySource.WAL
    if sidecar_label == RecoverySource.JOURNAL.value:
        return RecoverySource.JOURNAL
    return RecoverySource.UNKNOWN


def _carve_recovery_source(carve_tag: str) -> RecoverySource:
    """The ``RecoverySource`` for a carved row: FREELIST or FREESPACE by tag, UNKNOWN for
    any unrecognised tag.

    WHY a fallback rather than ``RecoverySource(carve_tag)``: the direct enum lookup RAISES
    on an unknown value, which — inside the per-row carve loop — would abort the whole read
    and lose the already-merged LIVE rows. A future carve tag must degrade to UNKNOWN, not
    kill the read (mirrors ``_sidecar_recovery_source``)."""
    if carve_tag == RecoverySource.FREELIST.value:
        return RecoverySource.FREELIST
    if carve_tag == RecoverySource.FREESPACE.value:
        return RecoverySource.FREESPACE
    return RecoverySource.UNKNOWN


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
    # A row is keyed by column NAME (dict), so two output columns sharing a name would
    # silently collapse under dict(zip(...)) — one side's data lost. This happens on a
    # self-join SELECT * whose table-qualified naming (see harden_connection) still repeats
    # a name — e.g. an old build where full_column_names is a no-op, or duplicate literals
    # like ``SELECT id, id``. Reject it with a clear error rather than drop a column: a
    # faithful extractor must never silently discard evidence, and (AS being forbidden) the
    # user's recourse is to project the columns explicitly.
    duplicates = sorted({name for name, count in Counter(columns).items() if count > 1})
    if duplicates:
        raise ValueError(
            f"query returned duplicate output column name(s) {duplicates}: two columns "
            f"with the same name would collapse and lose data. Project the columns "
            f"explicitly so every output name is unique (renaming with AS is not allowed)."
        )
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
    requires_subunit = True   # a read needs a table (or an explicit params['sql'])

    def subunits(self, source: Path) -> list[str]:
        try:
            with contextlib.closing(_connect(source, immutable=True)) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                return [str(row[0]) for row in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001 - a probe failure must never abort matching
            # WARNING, not debug: an unlistable db is the operator's answer to "why was this
            # file left unread" (encrypted keychain, truncated carve, wrong magic). The
            # writer reports the file as unread; only this line names the underlying cause.
            log.warning(f"cannot list the tables of {source.name}: {exc}")
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

        # dtype=object on BOTH frames: without it, an integer column carrying a NULL in
        # one pass is promoted int64 -> float64 by pandas, which loses precision above
        # 2**53 (a large id would collide with its neighbour, and a committed row would
        # mis-match its own sidecar re-read). Object dtype keeps every value as its native
        # Python type (int stays int, None stays None), so the merge compares true values
        # and an integer cell is yielded as an int, never a float. Both frames MUST share
        # the dtype so the two passes align — see model_atlas.sqlite.dedup.
        merged = merge_with_source(
            pd.DataFrame(db_only_rows, columns=columns, dtype=object),
            pd.DataFrame(with_sidecars_rows, columns=columns, dtype=object),
            sidecar_label=sidecar_label,
        )

        # Split back into live vs recovered, preserving each half's own order, then hand
        # BOTH to numbered_records — rule 1 owns "live first, recovered after, one
        # continuing sequence", never re-derived here.
        live: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []
        sidecar_source = _sidecar_recovery_source(sidecar_label)
        for record in merged.to_dict(orient="records"):
            origin = record.pop(SOURCE_COLNAME)
            record.pop(SOURCE_ROW_NUMBER_COLNAME)
            # merge_with_source labels every row 'both' / 'db_only_unique' (live) or
            # sidecar_label (recovered) — never any other value. Each row carries its fine
            # recovery_source (wal/journal for a sidecar row, live otherwise) through to the
            # yield below via the private key numbered_records passes through untouched.
            if origin == sidecar_label:
                record[_RECOVERY_SOURCE_KEY] = sidecar_source
                recovered.append(record)
            else:
                record[_RECOVERY_SOURCE_KEY] = RecoverySource.LIVE
                live.append(record)

        # An ADDITIONAL recovery pass. Rows carved from the main file's unallocated
        # space (freelist + leaf free space) are appended AFTER the sidecar-recovered
        # rows, so numbered_records numbers all recovered rows — sidecar then carved — in
        # one continuing sequence behind the live rows. Only the table path carves (a
        # custom SQL join has no single schema); the carver reads `source`'s raw bytes,
        # never the staging copy. The carver's freelist/freespace tag becomes the row's
        # recovery_source via _carve_recovery_source (an unknown tag -> UNKNOWN, never a
        # raise), and the whole pass is isolated so a carve failure cannot lose live rows.
        if table is not None and params.get("carve", True):
            # Carving is BEST-EFFORT and ADDITIONAL. Any failure — a table name that will
            # not pass carve's strict identifier quoting, a corrupt page-size header, an
            # unexpected byte layout — must degrade to "no carved rows" and NEVER lose the
            # live + sidecar-recovered rows already merged above. Losing recoverable
            # deleted rows is regrettable; losing LIVE evidence to a carve bug is not
            # acceptable, so the whole pass is isolated. (carve_table returns a fully
            # materialised list, so the call runs to completion inside this guard.)
            try:
                carved_rows = carve_table(source, str(table), columns=columns)
            except Exception as exc:  # noqa: BLE001 - best-effort recovery must not abort the read
                log.warning(
                    f"carve pass failed for table {table!r} in {source}: {exc} — "
                    f"continuing with live + sidecar-recovered rows only"
                )
                carved_rows = []
            for carved in carved_rows:
                carved[_RECOVERY_SOURCE_KEY] = _carve_recovery_source(
                    carved.pop(CARVE_SOURCE_COLNAME)
                )
                recovered.append(carved)

        for line_no, fields, is_recovered in numbered_records(live, recovered):
            recovery_source = fields.pop(_RECOVERY_SOURCE_KEY)
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
                recovery_source=recovery_source,
            )

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        """Columns of ``subunit`` (a table name), or None when they cannot be verified.

        Security invariant: ``quote_identifier`` is the ONLY path a table name reaches SQL
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
