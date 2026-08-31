"""The RunStore: a temp on-disk SQLite run database with a layer-typed write API.

Defines:    ``RunStore`` — owns a TEMP ON-DISK SQLite database (a real file in a tempdir,
            ``journal_mode=OFF`` / ``synchronous=OFF``), applies the schema on open,
            and exposes ONE write method per run-DB table, grouped by layer
            (L0 acquisition / L1 observation / L2 integration / L3 analysis). Persists with
            ``VACUUM INTO`` (``persist``), exposes the live ``connection`` (read-only
            consumers/exporters) and cleans up the temp file on ``close`` / context-manager
            exit. Also exposes the introspectable ``WRITER_TABLE`` / ``TABLE_LAYER`` surface.
Used by:    the ingestion/assembly bricks (they build the family dataclasses and call
            the writers in parent-before-child order, so foreign keys resolve),
            read-only consumers/exporters (via ``.connection``), and tests.core.test_store.
Depends on: ``model_atlas.core.schema`` (``create_schema`` — the derived DDL + the
            versioning stamps), ``model_atlas.core.vocabularies`` (the frozen family
            dataclasses the writers accept), and the standard library only (``sqlite3``,
            ``dataclasses``, ``enum``, ``pathlib``, ``tempfile``).

Storage decision (settled 2026-07-11)
--------------------------------------
The run DB is **reproducible scratch**: everything in it is a deterministic function of
the evidence + presets, so crash-safety is not a fidelity property. We therefore open a
real temp file with ``journal_mode=OFF`` and ``synchronous=OFF`` (no rollback journal, no
fsync) — the fastest durable-enough mode for a throwaway DB — and persist a clean copy on
demand with ``VACUUM INTO``. This deliberately REPLACES the old ``sqlite3.backup()`` path
and its ``isolation_level=None`` autocommit hack (see ``model_atlas/model/store.py``): the
backup API retried ``SQLITE_BUSY`` forever if a write transaction was open, forcing the
store to stay in autocommit. ``VACUUM INTO`` has no such constraint — it only needs the
connection to hold no open transaction, which ``persist`` guarantees with a single
``commit()`` — so that whole deadlock constraint is dissolved and MUST NOT be reintroduced.

Layer discipline (principle 6 — the store enforces the write API split)
-----------------------------------------------------------------------
The store is the only object the bricks share, so its API is where the L0/L1/L2/L3 split is
made structural. There is deliberately **no generic public ``write(table, row)``**: each
public writer names exactly one table (a literal, mirrored in ``WRITER_TABLE``) drawn from
exactly one layer's table set, so an L0/L1 writer is structurally unable to touch an L2/L3
table and vice-versa. The single generic mechanism, ``_insert``, is private.

No silent loss
--------------
There is **no ``INSERT OR IGNORE`` anywhere**: a UNIQUE violation is a derivation bug and
must surface, so ``sqlite3.IntegrityError`` is left to propagate. The one content-keyed
convenience is ``write_preset`` (a preset's definition IS its identity):
SELECT-then-INSERT, returning the existing id for an identical row but RAISING when the
same ``key``+``version`` carries a different ``definition_digest`` (the un-bumped-edit
finding). The parent-before-child ingest write order (case → run → source → raw_record →
activity(+record) → entity + expressions + components BEFORE the observation referencing
them → integrations → analysis) is NOT enforced by the store: ``PRAGMA foreign_keys = ON``
(set by ``create_schema``) makes every FK check immediately, so writing a child before its
parent raises on its own.
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from model_atlas.core import vocabularies as V
from model_atlas.core.schema import APPLICATION_ID, USER_VERSION, create_schema

# --- the layer partition of the write API ----------------------------------
# One tuple per layer; the write API is partitioned by these. Each public writer targets
# exactly one table from exactly one of these sets, so the L0/L1/L2/L3 split is structural
# (there is no generic public write). ``TABLE_LAYER`` and ``WRITER_TABLE`` below make that
# surface introspectable so a test can assert it directly (never a runtime guard — the
# guarantee is by construction).

L0_TABLES: tuple[str, ...] = (
    "case_metadata", "run", "archive", "run_archive", "preset", "run_preset",
    "source", "raw_record",
)
L1_TABLES: tuple[str, ...] = (
    "activity", "activity_record", "entity", "spatial_expression",
    "spatial_component", "temporal_expression", "temporal_component", "observation",
)
L2_TABLES: tuple[str, ...] = (
    "spatial_integration", "timespan_integration", "spatial_index",
)
L3_TABLES: tuple[str, ...] = ("analysis", "edit_log")

# table -> its layer label. Built once from the four sets so the layer of a table has a
# single source of truth (a table listed in two sets would be a bug caught here at import).
TABLE_LAYER: dict[str, str] = {}
for _layer, _tables in (("L0", L0_TABLES), ("L1", L1_TABLES),
                         ("L2", L2_TABLES), ("L3", L3_TABLES)):
    for _table in _tables:
        assert _table not in TABLE_LAYER, f"table {_table!r} listed in two layers"
        TABLE_LAYER[_table] = _layer


def _authenticate_run_db(conn: sqlite3.Connection, path: Path) -> None:
    """Reject a connection that is not a genuine mATLAS run database.

    HOW: read the two versioning stamps the schema writes (``application_id`` /
    ``user_version``) and require the single ``matlas_meta`` marker table with at least one
    row. WHY: opening an existing file for editing or analysis touches a forensic artefact,
    so a foreign SQLite file (or a wrong-generation DB) must fail loudly here rather than be
    read or written as if it were ours.
    """
    # Close-on-reject guarantee: ANY failure here — a ValueError reject OR the
    # sqlite3.DatabaseError the very first PRAGMA raises on a garbage/truncated file (which
    # is not a valid SQLite database at all) — must close the connection before propagating,
    # or open_persisted() leaks the fd/handle on the malformed-file path (contradicting the
    # docstring). Wrapping the whole probe means every exit path frees the connection.
    try:
        application_id = conn.execute("PRAGMA application_id;").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version;").fetchone()[0]
        if application_id != APPLICATION_ID or user_version != USER_VERSION:
            raise ValueError(
                f"{path} is not a mATLAS run database (application_id={application_id:#x}, "
                f"user_version={user_version}; expected {APPLICATION_ID:#x} / {USER_VERSION})")
        has_meta = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'matlas_meta';"
        ).fetchone()[0]
        if has_meta != 1 or conn.execute("SELECT COUNT(*) FROM matlas_meta;").fetchone()[0] < 1:
            raise ValueError(f"{path} carries the mATLAS stamps but no matlas_meta row")
    except Exception:
        conn.close()
        raise


def _bind(value: Any) -> Any:
    """Render one dataclass field value for the sqlite3 driver.

    HOW: a vocabulary member (every one is a ``str``-Enum) becomes its bare ``.value`` — the
    token the schema's CHECK constraints and the demo store — everything else (str, int,
    float, None) binds unchanged. WHY normalise here rather than trust str-Enum's str
    identity: it keeps the STORED form the bare vocabulary token in one place, so a future
    non-str Enum could not silently store a ``repr`` like ``"EntityRole.CALLER"``.
    """
    if isinstance(value, Enum):
        return value.value
    return value


class RunStore:
    """A temp on-disk SQLite run database with a layer-typed, no-silent-loss write API.

    Open one per run (``RunStore()``), write rows in parent-before-child order through
    the layer methods, then ``persist(path)`` a clean copy. Use as a context manager so
    the temp file is always removed::

        with RunStore() as store:
            store.write_case_metadata(case)
            ...
            store.persist(out_path)
    """

    # The introspectable contract: public writer method name -> the ONE table it writes.
    # Mirrors the literals inside each method; a test asserts every write_* method appears
    # here and maps to a single table in a single layer (the by-construction layer split).
    WRITER_TABLE: dict[str, str] = {
        # L0 — acquisition
        "write_case_metadata": "case_metadata",
        "write_run": "run",
        "write_archive": "archive",
        "write_run_archive": "run_archive",
        "write_preset": "preset",
        "write_run_preset": "run_preset",
        "write_source": "source",
        "write_raw_record": "raw_record",
        # L1 — observation
        "write_activity": "activity",
        "write_activity_record": "activity_record",
        "write_entity": "entity",
        "write_spatial_expression": "spatial_expression",
        "write_spatial_component": "spatial_component",
        "write_temporal_expression": "temporal_expression",
        "write_temporal_component": "temporal_component",
        "write_observation": "observation",
        # L2 — integration
        "write_spatial_integration": "spatial_integration",
        "write_timespan_integration": "timespan_integration",
        "write_spatial_index": "spatial_index",
        # L3 — analysis
        "write_analysis": "analysis",
        "write_edit_log": "edit_log",
    }

    def __init__(self) -> None:
        # A REAL temp file (not ``:memory:``): VACUUM INTO copies a file DB, and an on-disk
        # DB survives the process for debugging a failed run. mkstemp returns an os-level fd
        # we do not need (sqlite3 opens the path itself), so close it immediately.
        fd, path = tempfile.mkstemp(suffix=".matlas-run.sqlite")
        os.close(fd)
        self._path = Path(path)
        self._conn = sqlite3.connect(self._path)
        # This store OWNS a scratch temp file, so close() deletes it. The persisted-DB opener
        # below clears this flag: an existing run DB is evidence, never deleted on close.
        self._owns_temp = True
        # Tracks whether close() has already run, so a second close()/context-exit is a no-op
        # (close() now commits, and commit() on an already-closed connection would raise).
        self._closed = False
        # Reproducible scratch: no rollback journal, no fsync — crash-safety is not a
        # fidelity property here, and the run DB is a deterministic function of the inputs.
        self._conn.execute("PRAGMA journal_mode = OFF;")
        self._conn.execute("PRAGMA synchronous = OFF;")
        # create_schema turns on foreign_keys, stamps user_version/application_id, builds the
        # DDL and writes the single matlas_meta row (then commits).
        create_schema(self._conn)

    @classmethod
    def open_persisted(cls, path: str | Path, *, read_only: bool = False) -> "RunStore":
        """Attach to an EXISTING persisted run DB (a ``<name>.matlas.sqlite``).

        Unlike the constructor, which mints a fresh scratch database, this opens a real,
        already-written run database — the file an operator edits (``read_only=False``) or an
        analysis reads (``read_only=True``) — and authenticates it as a genuine mATLAS run DB
        before returning. The file is NEVER deleted on ``close()``: it is evidence, not
        scratch. The full write API is available on a read-write handle; a read-only handle
        exposes ``.connection`` for queries and rejects writes at the SQLite level.
        """
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(f"no such mATLAS run DB: {target}")
        if read_only:
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(target)
        # The model relies on FK enforcement; turn it on before any read or write.
        conn.execute("PRAGMA foreign_keys = ON;")
        _authenticate_run_db(conn, target)   # raises + closes on a non-mATLAS file
        store = cls.__new__(cls)             # bypass __init__: no temp file is created here
        store._path = target
        store._conn = conn
        store._owns_temp = False             # an existing run DB is never deleted on close
        store._closed = False
        return store

    # --- the single private mechanism -------------------------------------

    def _insert(self, table: str, row: object) -> None:
        """INSERT one frozen family dataclass into ``table`` (columns = its field names).

        HOW: the dataclass field names ARE the column names (the schema derives its columns
        from the same dataclasses), so a positional ``fields()`` walk gives an exact,
        order-independent column list and value tuple; enums are lowered by ``_bind``.
        WHY private with a literal ``table`` at every call site: this is the ONLY generic
        writer, and keeping it private is what makes the public layer split structural — a
        caller cannot reach an out-of-layer table because no public method accepts a table.
        No ``INSERT OR IGNORE``: a UNIQUE/FK violation is a derivation bug and must raise.
        """
        columns = [f.name for f in dataclasses.fields(row)]
        placeholders = ", ".join("?" for _ in columns)
        values = tuple(_bind(getattr(row, name)) for name in columns)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )

    # --- Layer 0: acquisition ---------------------------------------------

    def write_case_metadata(self, row: V.CaseMetadata) -> None:
        self._insert("case_metadata", row)

    def write_run(self, row: V.Run) -> None:
        self._insert("run", row)

    def write_archive(self, row: V.Archive) -> None:
        self._insert("archive", row)

    def write_run_archive(self, row: V.RunArchive) -> None:
        self._insert("run_archive", row)

    def write_preset(self, row: V.Preset) -> str:
        """Content-keyed insert: a preset's definition IS its identity.

        HOW: SELECT on the natural key ``(key, version)`` first; if a row exists with the
        SAME ``definition_digest`` return its id (the preset was legitimately selected/applied
        again — not a derivation bug); if it exists with a DIFFERENT digest RAISE; otherwise
        INSERT and return the new id.
        WHY this is the ONE exception to "no content-keyed insert / no OR IGNORE": re-applying
        an identical preset must not raise, but a changed definition under an UN-BUMPED version
        is exactly the un-bumped-preset-edit finding the model exists to surface — so it must
        raise loudly, never be silently ignored or merged.
        """
        existing = self._conn.execute(
            "SELECT preset_id, definition_digest FROM preset WHERE key = ? AND version IS ?",
            (row.key, row.version),
        ).fetchone()
        if existing is not None:
            existing_id, existing_digest = existing
            # Invariant: same key+version but a different digest means the definition
            # changed without a version bump — a surfaced finding, never a silent overwrite.
            if existing_digest != row.definition_digest:
                raise ValueError(
                    f"preset {row.key!r} version {row.version!r} already recorded with a "
                    f"different definition_digest ({existing_digest!r} != "
                    f"{row.definition_digest!r}): an un-bumped preset edit")
            return existing_id
        self._insert("preset", row)
        return row.preset_id

    def write_run_preset(self, row: V.RunPreset) -> None:
        self._insert("run_preset", row)

    def write_source(self, row: V.Source) -> None:
        self._insert("source", row)

    def write_raw_record(self, row: V.RawRecord) -> None:
        self._insert("raw_record", row)

    # --- Layer 1: observation -----------------------------------------------

    def write_activity(self, row: V.Activity) -> None:
        self._insert("activity", row)

    def write_activity_record(self, row: V.ActivityRecord) -> None:
        self._insert("activity_record", row)

    def write_entity(self, row: V.Entity) -> None:
        self._insert("entity", row)

    def write_spatial_expression(self, row: V.SpatialExpression) -> None:
        self._insert("spatial_expression", row)

    def write_spatial_component(self, row: V.SpatialComponent) -> None:
        self._insert("spatial_component", row)

    def write_temporal_expression(self, row: V.TemporalExpression) -> None:
        self._insert("temporal_expression", row)

    def write_temporal_component(self, row: V.TemporalComponent) -> None:
        self._insert("temporal_component", row)

    def write_observation(self, row: V.Observation) -> None:
        self._insert("observation", row)

    # --- Layer 2: integration ---------------------------------------------

    def write_spatial_integration(self, row: V.SpatialIntegration) -> None:
        self._insert("spatial_integration", row)

    def write_timespan_integration(self, row: V.TimespanIntegration) -> None:
        self._insert("timespan_integration", row)

    def write_spatial_index(self, row: V.SpatialIndex) -> None:
        self._insert("spatial_index", row)

    # --- Layer 3: analysis ------------------------------------------------

    def write_analysis(self, row: V.Analysis) -> None:
        self._insert("analysis", row)

    def write_edit_log(self, row: V.EditLog) -> None:
        self._insert("edit_log", row)

    # --- persistence + lifecycle ------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The live connection, for READ-ONLY consumers/exporters (they never write here)."""
        return self._conn

    def persist(self, path: str | Path) -> Path:
        """Write a clean, standalone copy of the run DB to ``path`` and return it.

        HOW: ``VACUUM INTO`` writes a fresh, defragmented database file carrying every table,
        index, view AND the versioning pragmas (user_version/application_id). It requires no
        open transaction, so we ``commit()`` first (flushing any pending writes); an existing
        target is removed first because ``VACUUM INTO`` refuses to overwrite a non-empty file.
        WHY not ``sqlite3.backup()``: the backup API's busy-retry loop deadlocked against an
        open write transaction, forcing the old store into autocommit — a constraint the
        reproducible-scratch storage decision above dissolved. ``VACUUM INTO`` has no such
        hazard, so it is the only persist path.
        """
        target = Path(path)
        if target.exists():
            target.unlink()
        self._conn.commit()
        # Inline the path (VACUUM INTO takes an SQL string expression, not a bind parameter);
        # double any single quote so a path with a quote cannot break out of the literal.
        escaped = str(target).replace("'", "''")
        self._conn.execute(f"VACUUM INTO '{escaped}'")
        return target

    def close(self) -> None:
        """Flush pending writes, close the connection; delete the file only for a scratch temp DB.

        HOW: ``commit()`` before ``close()`` so any writes made directly through an
        ``open_persisted(read_only=False)`` handle are durable (on a read-only handle it is a
        harmless no-op). WHY: the connection is not autocommit, so ``close()`` without a commit
        would silently ROLL BACK uncommitted direct writes — a forensic store must never drop a
        write on the floor. ``persist()`` commits independently, so this does not affect the
        VACUUM INTO path (and reintroduces no backup()/autocommit constraint).
        A store opened over an EXISTING persisted run DB (``open_persisted``) keeps the file —
        it is evidence. Idempotent."""
        # Idempotent guard: a second close() (e.g. explicit close() then context-exit) must not
        # commit()/close() again — commit() on an already-closed connection raises.
        if self._closed:
            return
        self._closed = True
        self._conn.commit()
        self._conn.close()
        if self._owns_temp:
            # missing_ok: close may run after a failed __init__ or a second call; a scratch run
            # DB is disposable, so a missing file is never an error.
            self._path.unlink(missing_ok=True)

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
