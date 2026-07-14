"""SQLite-backed run store: the in-memory relational core, projected to the flat frame.

Defines:    the three row types (SourceRow, RecordRow, AssertionRow) and RunStore, the
            Phase-C container that owns an in-memory SQLite database (the schema from
            model.schema), INSERTs the typed six-family assertions into the relational
            tables, and projects them back to today's flat 45-column frame via the
            ``assertion_flat`` view (``to_flat_frame``); plus the single
            ``passthrough_column_names`` helper (the ``orig_<col>`` naming).
Used by:    transforms.assemble (``build_assertions`` writes rows into it); the pipeline
            (Phase C: one run-wide store per run, the ``run``/``source`` rows populated up
            front, ranks written via ``write_ranks``, the whole DB read via ``to_flat_frame``
            and persisted via ``persist`` / the ``sqlite`` exporter's connection backup).
Depends on: model.families (OUTPUT_COLUMNS, SpatioTemporalAssertion.to_flat_row), model.schema
            (create_schema, entity_id_for, POSITION_COLUMNS, TIMESPAN_COLUMNS), pandas,
            standard library (sqlite3, json, uuid is not used here directly).

Phase C of the relational-core migration (``docs/relational_model.md``): the store IS the
in-memory SQLite database. Entities ARE deduplicated (uuid5 over ``label|entity_type``);
each assertion owns its position/timespan corner rows keyed by its own ``row_uid``.
``attribution_origin`` is stored on the assertion table but is deliberately absent from the
flat frame (it is not an OUTPUT_COLUMNS member — CSV parity). The critical invariant this
phase preserves is that ``to_flat_frame`` reproduces the previous flat frame byte-for-byte
(the golden pipeline tests are the oracle); the numeric columns stored as TEXT because their
model type is a union (``utc_offset_hours``, ``time_accuracy_us``) are re-coerced to a numeric
dtype here so the CSV rendering is unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from model_atlas.model.families import OUTPUT_COLUMNS, SpatioTemporalAssertion
from model_atlas.model.schema import (
    POSITION_COLUMNS,
    TIMESPAN_COLUMNS,
    create_schema,
    entity_id_for,
)


# Default prefix for verbatim source-column passthrough. NOT ``raw_`` — that is already
# used by canonical columns (``raw_position``, ``raw_source_path``). Overridable per run via
# ``matlas_config.toml [output].source_column_prefix`` (threaded through the pipeline).
# Consumed by: RunStore.to_flat_frame and transforms.assemble (the passthrough default).
PASSTHROUGH_PREFIX = "orig_"

# The flat-frame keys (OUTPUT_COLUMNS names) that back each timespan table column. The bound
# columns re-apply the ``time_`` prefix the flat frame carries; the scalar Temporal fields do
# not. Derived by name here so the store and the schema view stay in step.
_TIMESPAN_FLAT_KEYS: dict[str, str] = {
    "lower_raw": "time_lower_raw",
    "lower_source_field": "time_lower_source_field",
    "lower_unix_utc_us": "time_lower_unix_utc_us",
    "upper_raw": "time_upper_raw",
    "upper_source_field": "time_upper_source_field",
    "upper_unix_utc_us": "time_upper_unix_utc_us",
    "utc_offset_hours": "utc_offset_hours",
    "time_accuracy_us": "time_accuracy_us",
    "temporal_source": "temporal_source",
}

# Columns whose MODEL type is a numeric union (so the schema stores them as TEXT) but whose
# values are semantically numeric. After reading the flat view back, their dtype is restored
# to numeric so the CSV renders them unquoted exactly as the pre-SQLite dict-built frame did.
# ``utc_offset_hours`` is ``float | str | None`` (the "N/A" sentinel keeps it TEXT);
# ``time_accuracy_us`` is ``int | float | None``.
_NUMERIC_TEXT_COLUMNS: tuple[str, ...] = ("utc_offset_hours", "time_accuracy_us")


def passthrough_column_names(source_fields: list[str], prefix: str) -> dict[str, str]:
    """Map each source field to its ``<prefix><field>`` output column name, preserving source order.

    A name that would collide with a canonical column is disambiguated with a trailing
    ``_``. The mapping is a pure function of the field name, so it is identical across
    sources (stable merges). This is the SINGLE implementation of the passthrough naming;
    ``transforms.assemble`` imports it rather than re-deriving it.
    """
    used = set(OUTPUT_COLUMNS)
    names: dict[str, str] = {}
    for col in source_fields:
        name = f"{prefix}{col}"
        while name in used:
            name += "_"
        names[col] = name
        used.add(name)
    return names


def _bind(value: Any) -> Any:
    """A value the sqlite3 driver can bind. Numpy scalars (which the driver rejects) are
    unwrapped to their python native; everything else passes through unchanged.

    WHY: the source-original ``raw`` cells (time bounds, raw_position) may arrive as numpy
    scalars from the pandas readers; binding one raises, so it is unwrapped here. This changes
    only the STORED type of a free-form passthrough cell, never a canonical numeric column
    (those are already python scalars from the assembly cast).
    """
    if isinstance(value, np.generic):
        return value.item()
    return value


@dataclass(frozen=True)
class SourceRow:
    """One matched ``(file, selector, preset)`` extraction.

    ``source_id`` is deterministic (uuid5 over ``fingerprint|raw_source_path|preset_id``)
    so the same source read from a folder or a zip yields the same id. Backs the SOURCE
    table row; the flat view projects a subset of it (raw_source_path, input_file_path,
    preset_id/name, source_tier).
    """

    source_id: str
    input_file_path: str | None = None
    container_chain: str | None = None      # human-readable outer..inner container path
    raw_source_path: str | None = None
    source_fingerprint: str | None = None
    format: str | None = None
    subunit: str | None = None      # table / sheet / query the rows were read from
    preset_id: str | None = None
    preset_name: str | None = None
    source_tier: str | None = None
    record_count: int | None = None         # source records read
    assertion_count: int | None = None      # output assertions the records fanned out into


@dataclass(frozen=True)
class RecordRow:
    """One source record, verbatim. ``raw`` is the COMPLETE source row (the exploded
    ``orig_<col>`` passthrough columns are re-emitted from it, so a name that collides
    with a canonical column survives). ``recovery_state`` is engine enrichment (live /
    wal / journal / ...), NOT a CSV column this phase."""

    source_record_uid: str
    source_id: str
    line_no: int                    # physical 1-based line within the source
    input_record_id: str
    recovery_state: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class AssertionRow:
    """One output row = one reified ternary fact. ``attribution_origin``
    (``operator_param`` | ``preset_mapped``) records who asserted the
    entity->linked_entity attribution; it is stored on the assertion table but MUST NOT
    appear in the flat frame (byte-parity requirement — it is not an OUTPUT_COLUMNS member)."""

    row_uid: str
    source_record_uid: str
    assertion: SpatioTemporalAssertion
    attribution_origin: str


class RunStore:
    """Owns the in-memory relational run database and projects it to the flat frame.

    The store creates the schema on init and INSERTs each source / record / assertion into
    the relational tables. Insertion order is preserved by the tables' rowids, so the flat
    view (ordered by the assertion rowid) reproduces the previous engine's row order.
    """

    def __init__(self) -> None:
        # An in-memory database per run. check_same_thread is left at its default (the
        # store is used from one thread); a persisted copy is taken on demand via persist().
        # isolation_level=None (autocommit) is REQUIRED, not a preference: under the legacy
        # implicit-transaction mode an INSERT leaves a write transaction open, and
        # sqlite3.Connection.backup() then retries SQLITE_BUSY forever (persist() hangs).
        # Autocommit also lets a separate read-only connection observe ingested rows.
        self._conn = sqlite3.connect(":memory:", isolation_level=None)
        create_schema(self._conn)

    # --- ingestion ---------------------------------------------------------

    def add_source(self, row: SourceRow) -> None:
        # INSERT OR IGNORE: a source read once per run, but a defensive no-op on a repeat id
        # keeps the store idempotent rather than raising on a duplicate primary key.
        self._conn.execute(
            "INSERT OR IGNORE INTO source (source_id, input_file_path, container_chain, "
            "raw_source_path, fingerprint, format, subunit, preset_id, preset_name, "
            "source_tier, record_count, assertion_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.source_id, row.input_file_path, row.container_chain,
                row.raw_source_path, row.source_fingerprint, row.format, row.subunit,
                row.preset_id, row.preset_name, row.source_tier,
                row.record_count, row.assertion_count,
            ),
        )

    def add_run(
        self, *, run_id: str, started_at: str | None, tool_version: str | None,
        settings_json: str | None, finished_at: str | None = None,
    ) -> None:
        """Register the single run row (its resolved settings snapshot lives in ``settings_json``).

        Populated up front from the run's ``started_at``, the tool ``__version__`` and the
        resolved settings; ``finished_at`` is left NULL here (the run is still in progress).
        INSERT OR IGNORE keeps a defensive no-op should it ever be called twice.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO run (run_id, started_at, finished_at, tool_version, "
            "settings_json) VALUES (?, ?, ?, ?, ?)",
            (run_id, started_at, finished_at, tool_version, settings_json),
        )

    def add_record(self, row: RecordRow) -> None:
        # raw_json is the complete source row verbatim; default=str renders any value the JSON
        # encoder cannot (datetimes, numpy scalars) so serialisation never fails. The
        # ``orig_<col>`` passthrough is re-exploded from this in to_flat_frame.
        raw_json = json.dumps(row.raw, ensure_ascii=False, default=str)
        self._conn.execute(
            "INSERT OR IGNORE INTO record (source_record_uid, source_id, line_no, "
            "input_record_id, recovery_state, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.source_record_uid, row.source_id, row.line_no,
                row.input_record_id, row.recovery_state, raw_json,
            ),
        )

    def add_assertion(self, row: AssertionRow) -> None:
        """Insert one assertion and its owned corner rows (entity ×2, position, timespan).

        HOW: the assertion's flat projection supplies every value (enums already stringified
        by ``to_flat_row``); the entity corners are deduplicated by their deterministic id, the
        position/timespan corners are keyed by this assertion's ``row_uid`` and created only
        when they carry at least one non-null value.

        WHY INSERT OR IGNORE on every row here: ``row_uid`` is content-addressed and portable
        (the SAME source content read from a folder or from a zip yields the same uid — see
        transforms.assemble). Two byte-identical acquisitions (e.g. the same database inside two
        different zips) therefore produce the same assertion: the store deduplicates it rather
        than raising a primary-key collision. This makes the run-wide store idempotent, matching
        ``add_source`` / ``add_record``. Within one source the line number + output ordinal keep
        every genuine row distinct, so OR IGNORE never merges two different assertions.
        """
        flat = row.assertion.to_flat_row()
        row_uid = row.row_uid

        # Entity (the trace-bearing thing) — always present as a corner; deduplicated.
        entity_id = entity_id_for(flat["entity"], flat["entity_type"])
        self._conn.execute(
            "INSERT OR IGNORE INTO entity (entity_id, label, entity_type) VALUES (?, ?, ?)",
            (entity_id, flat["entity"], flat["entity_type"]),
        )
        # Linked entity (the attributed actor) — a second role of the same table, nullable.
        # It has no entity_type in the model, so it is keyed on ``label|None``.
        linked_label = flat["linked_entity"]
        linked_entity_id: str | None = None
        if linked_label is not None:
            linked_entity_id = entity_id_for(linked_label, None)
            self._conn.execute(
                "INSERT OR IGNORE INTO entity (entity_id, label, entity_type) VALUES (?, ?, ?)",
                (linked_entity_id, linked_label, None),
            )

        # Position corner — only when the assertion actually carries spatial data.
        position_values = [flat[name] for name, _ in POSITION_COLUMNS]
        if any(value is not None for value in position_values):
            names = ", ".join(name for name, _ in POSITION_COLUMNS)
            placeholders = ", ".join("?" for _ in POSITION_COLUMNS)
            self._conn.execute(
                f"INSERT OR IGNORE INTO position (position_id, {names}) VALUES (?, {placeholders})",
                (row_uid, *(_bind(value) for value in position_values)),
            )

        # Timespan corner — only when the assertion carries temporal data.
        timespan_values = [flat[_TIMESPAN_FLAT_KEYS[name]] for name, _ in TIMESPAN_COLUMNS]
        if any(value is not None for value in timespan_values):
            names = ", ".join(name for name, _ in TIMESPAN_COLUMNS)
            placeholders = ", ".join("?" for _ in TIMESPAN_COLUMNS)
            self._conn.execute(
                f"INSERT OR IGNORE INTO timespan (timespan_id, {names}) VALUES (?, {placeholders})",
                (row_uid, *(_bind(value) for value in timespan_values)),
            )

        # The assertion itself. record_type / record_rank are None here (untangle writes them
        # later via write_ranks); source_label is per-assertion Provenance (no source-table home).
        self._conn.execute(
            "INSERT OR IGNORE INTO assertion (row_uid, source_record_uid, entity_id, linked_entity_id, "
            "entity_position_link, entity_time_link, spatial_temporal_link, attribution_origin, "
            "raw_source_path, source_label, source_tier, deleted, record_type, record_rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_uid, row.source_record_uid, entity_id, linked_entity_id,
                flat["entity_position_link"], flat["entity_time_link"], flat["spatial_temporal_link"],
                row.attribution_origin, flat["raw_source_path"], flat["source_label"],
                flat["source_tier"], flat["deleted"], flat["record_type"], flat["record_rank"],
            ),
        )

    # --- untangle write-back ----------------------------------------------

    def write_ranks(self, pairs: Iterable[tuple[str, Any, Any]]) -> None:
        """Persist the untangle result: ``(row_uid, record_type, record_rank)`` per assertion.

        Untangle is computed once (on the flat frame) and frozen into the evidential record so
        every projection reads the same ranking; exporters never recompute it.
        """
        self._conn.executemany(
            "UPDATE assertion SET record_type = ?, record_rank = ? WHERE row_uid = ?",
            [(record_type, record_rank, row_uid) for row_uid, record_type, record_rank in pairs],
        )

    # --- projection --------------------------------------------------------

    def to_flat_frame(
        self, *, include_source_columns: bool, source_column_prefix: str
    ) -> pd.DataFrame:
        """Reproduce today's flat frame: OUTPUT_COLUMNS then the ``orig_<col>`` passthrough.

        The canonical columns come from ``SELECT * FROM assertion_flat``; the numeric-union
        TEXT columns are re-coerced to a numeric dtype (see ``_NUMERIC_TEXT_COLUMNS``) so the
        CSV rendering matches the pre-SQLite frame. The passthrough columns are exploded from
        each record's ``raw_json``; the passthrough column set is the UNION of every record's
        verbatim keys in first-appearance order across records (records are read in insertion
        order), so a run-wide store spanning several sources with differing schemas reproduces
        the former per-source-frame concatenation exactly (a row from a source lacking a column
        is simply blank there). An empty store yields an empty frame carrying only the canonical
        columns.
        """
        frame = pd.read_sql_query("SELECT * FROM assertion_flat", self._conn)
        for column in _NUMERIC_TEXT_COLUMNS:
            self._restore_numeric_dtype(frame, column)

        if not include_source_columns:
            return frame

        # Records in insertion order: the first record's keys fix the passthrough column order.
        record_rows = self._conn.execute(
            "SELECT source_record_uid, raw_json FROM record ORDER BY rowid"
        ).fetchall()
        raw_by_uid: dict[str, dict[str, Any]] = {
            uid: json.loads(raw_json) for uid, raw_json in record_rows
        }
        # Union every record's keys in first-appearance order (dicts preserve insertion order,
        # and record_rows is ORDER BY rowid), so several sources with differing schemas produce
        # the same passthrough column set/order as the former per-source-frame concatenation.
        source_fields: list[str] = []
        seen: set[str] = set()
        for raw in raw_by_uid.values():
            for key in raw:
                if key not in seen:
                    seen.add(key)
                    source_fields.append(key)
        names = passthrough_column_names(source_fields, source_column_prefix)
        if not names:
            return frame

        # Explode each row's verbatim record, so a fanned-out record repeats its passthrough
        # columns on every output row. Assigning a python list lets pandas infer the same dtype
        # the pre-SQLite dict-built frame produced (values are exactly as stored in raw_json).
        uids = frame["source_record_uid"].tolist()
        for col, name in names.items():
            frame[name] = [raw_by_uid[uid].get(col) for uid in uids]
        return frame

    @staticmethod
    def _restore_numeric_dtype(frame: pd.DataFrame, column: str) -> None:
        """Coerce a numeric-union TEXT column back to a numeric dtype where the dict-built frame
        would have. A column with at least one non-null value all of which parse as numbers
        becomes float64/int64 (rendered unquoted); an all-null column, or one holding a
        non-numeric sentinel (e.g. ``UTC_OFFSET_UNKNOWN`` "N/A"), is left as object so it renders
        quoted exactly as before.
        """
        series = frame[column]
        if not series.notna().any():
            return   # all-null -> object dtype -> quoted "" cells, matching the old frame
        try:
            frame[column] = pd.to_numeric(series)
        except (ValueError, TypeError):
            pass     # a non-numeric sentinel is present -> keep the text column verbatim

    # --- persistence -------------------------------------------------------

    def persist(self, path: Path) -> Path:
        """Back the in-memory run database up to ``path`` (the ``sqlite`` export target).

        Deletes an existing file first so no stale table/data survives across runs, then uses
        sqlite3's online backup to copy the whole database (all tables + the assertion_flat view).
        """
        path = Path(path)
        if path.exists():
            path.unlink()
        # backup() retries SQLITE_BUSY in a sleep loop with no timeout, so an open write
        # transaction on the source would hang for ever. The connection is in autocommit
        # mode, making this a no-op; it stays as the guard that keeps that invariant true.
        self._conn.commit()
        destination = sqlite3.connect(path)
        try:
            self._conn.backup(destination)
        finally:
            destination.close()
        return path

    @property
    def connection(self) -> sqlite3.Connection:
        """The store's live connection (exporters query it; the pipeline persists it)."""
        return self._conn
