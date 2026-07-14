"""SQLite DDL + assertion_flat view, derived one-directionally from families.py.

Defines:    the CREATE TABLE statements (run / source / record / entity / position /
            timespan / assertion), the CHECK constraints derived from the controlled-
            vocabulary enums, the ``assertion_flat`` VIEW projecting OUTPUT_COLUMNS, the
            deterministic id scheme (ENTITY_NAMESPACE + ``entity_id_for``), and
            ``create_schema`` which materialises the whole schema on a connection.
Used by:    model.store (the SQLite-backed RunStore creates the schema and inserts rows),
            tests.model.test_schema.
Depends on: model.families (the family dataclasses, the enums, OUTPUT_COLUMNS,
            UTC_OFFSET_UNKNOWN) and the standard library only (sqlite3, uuid, dataclasses,
            typing). No ORM: the derivation is one-way, families.py -> DDL.

Design (docs/relational_model.md, Phase C):
- Type mapping from the dataclass annotations: ``str`` -> TEXT, ``float`` -> REAL,
  ``int`` -> INTEGER, ``Any`` and genuine multi-type unions -> TEXT (``X | None`` is an
  Optional, mapped by its single non-None type). ``utc_offset_hours`` is therefore TEXT
  (it is ``float | str | None``: a real offset, the ``UTC_OFFSET_UNKNOWN`` "N/A" sentinel,
  or NULL); ``time_accuracy_us`` is TEXT for the same reason (``int | float | None``).
  ``RunStore.to_flat_frame`` restores the numeric dtype of these TEXT-stored numeric
  columns so the flat CSV stays byte-identical (see model.store).
- Corner ownership is literal: ``position_id`` == ``timespan_id`` == the owning
  assertion's ``row_uid``. A corner row exists only when the assertion has that corner;
  a positionless assertion simply has no position row and the view LEFT JOINs it to NULL.
- Entities ARE deduplicated: ``entity_id = uuid5(ENTITY_NAMESPACE, f"{label}|{entity_type}")``
  so the same identifier string is one entity row shared by every assertion about it.
- ``source_label`` (an OUTPUT_COLUMNS member) has no home in the design-doc tables; it is
  per-assertion Provenance, so it is stored on the ``assertion`` table and projected from
  there. ``attribution_origin`` is stored on ``assertion`` but is deliberately NOT projected
  by the view (CSV parity: it is not an OUTPUT_COLUMNS member).
"""
from __future__ import annotations

import sqlite3
import typing
import uuid
from dataclasses import fields as dataclass_fields
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from model_atlas.model.families import (
    OUTPUT_COLUMNS,
    EntityPositionLink,
    EntityTimeLink,
    RecordState,
    RecordType,
    SourceTier,
    Spatial,
    SpatialTemporalLink,
    Temporal,
    TemporalBound,
)

# Fixed namespace for the deterministic entity ids. A stable, arbitrary UUID (same
# philosophy as the two namespaces in transforms.assemble) so ``entity_id_for`` is
# reproducible across runs and machines. Consumed by: model.store (entity insertion).
ENTITY_NAMESPACE = uuid.UUID("9c4e7b1a-2d63-5f48-8a17-3b5c6d0e9f21")


def entity_id_for(label: Any, entity_type: Any) -> str:
    """The deterministic id of the entity ``(label, entity_type)``.

    HOW: uuid5 over ``f"{label}|{entity_type}"``, so identical ``(label, type)`` pairs
    collapse to one entity row (entities are deduplicated). WHY: the model never presumes
    two OBSERVATIONS coincide, but two references to the same identifier string ARE the
    same thing — value equality of an identifier is identity, unlike a position/time.
    """
    return str(uuid.uuid5(ENTITY_NAMESPACE, f"{label}|{entity_type}"))


# --- type derivation from the dataclass annotations ------------------------

def _sql_type(annotation: Any) -> str:
    """Map a dataclass field annotation to a SQLite column type.

    ``str`` -> TEXT, ``float`` -> REAL, ``int`` -> INTEGER; ``Optional[T]`` (``T | None``)
    maps by ``T``; ``Any`` and any genuine multi-type union -> TEXT. bool would map to
    INTEGER but no family field is bool.
    """
    origin = get_origin(annotation)
    if origin is typing.Union or origin is UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
    else:
        non_none = [annotation]
    if len(non_none) == 1:
        only = non_none[0]
        if only is str:
            return "TEXT"
        if only is float:
            return "REAL"
        if only is bool:
            return "INTEGER"
        if only is int:
            return "INTEGER"
    # typing.Any, or a genuine multi-type union (float|str, int|float): keep it verbatim.
    return "TEXT"


def _fields_of(dataclass_type: type) -> dict[str, Any]:
    """Field-name -> resolved annotation for a (``from __future__``-annotated) dataclass."""
    hints = get_type_hints(dataclass_type)
    return {f.name: hints[f.name] for f in dataclass_fields(dataclass_type)}


def _spatial_columns() -> list[tuple[str, str]]:
    """The position table's (name, sql_type) columns, derived from the Spatial family.

    The Spatial field names are exactly the OUTPUT_COLUMNS spatial names, so the position
    table columns and the view projection stay in lock-step with the single declaration.
    """
    return [(name, _sql_type(tp)) for name, tp in _fields_of(Spatial).items()]


def _temporal_columns() -> list[tuple[str, str]]:
    """The timespan table's (name, sql_type) columns, flattening the Temporal family.

    ``lower``/``upper`` (each a TemporalBound) flatten to ``<bound>_raw`` /
    ``<bound>_source_field`` / ``<bound>_unix_utc_us``; the scalar Temporal fields
    (``utc_offset_hours``, ``accuracy_us`` -> ``time_accuracy_us``, ``temporal_source``)
    follow. This mirrors the OUTPUT_COLUMNS temporal block (with the ``time_`` prefix
    re-applied only in the view, see TIMESPAN_VIEW_ALIASES).
    """
    bound_types = _fields_of(TemporalBound)   # raw, source_field, unix_us
    columns: list[tuple[str, str]] = []
    for bound in ("lower", "upper"):
        columns.append((f"{bound}_raw", _sql_type(bound_types["raw"])))
        columns.append((f"{bound}_source_field", _sql_type(bound_types["source_field"])))
        columns.append((f"{bound}_unix_utc_us", _sql_type(bound_types["unix_us"])))
    temporal_types = _fields_of(Temporal)
    columns.append(("utc_offset_hours", _sql_type(temporal_types["utc_offset_hours"])))
    columns.append(("time_accuracy_us", _sql_type(temporal_types["accuracy_us"])))
    columns.append(("temporal_source", _sql_type(temporal_types["temporal_source"])))
    return columns


# The position/timespan column lists, derived once at import.
POSITION_COLUMNS: list[tuple[str, str]] = _spatial_columns()
TIMESPAN_COLUMNS: list[tuple[str, str]] = _temporal_columns()


# --- CHECK-constraint helper -----------------------------------------------

def _enum_check(column: str, enum_type: type[Enum]) -> str:
    """A ``CHECK (col IN (...) OR col IS NULL)`` clause from an enum's values.

    NULL is always allowed: every controlled-vocabulary column is nullable today (an
    unmapped edge / state is None, rendered as an empty CSV cell), so the constraint must
    not reject it.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_type)
    return f"CHECK ({column} IN ({values}) OR {column} IS NULL)"


# --- table DDL -------------------------------------------------------------

def _create_table(name: str, columns: list[str]) -> str:
    body = ",\n    ".join(columns)
    return f"CREATE TABLE {name} (\n    {body}\n);"


def _run_table() -> str:
    return _create_table("run", [
        "run_id TEXT PRIMARY KEY",
        "started_at TEXT",
        "finished_at TEXT",
        "tool_version TEXT",
        "settings_json TEXT",
    ])


def _source_table() -> str:
    return _create_table("source", [
        "source_id TEXT PRIMARY KEY",
        "input_file_path TEXT",
        "container_chain TEXT",
        "raw_source_path TEXT",
        "format TEXT",
        "subunit TEXT",
        "fingerprint TEXT",
        "preset_id TEXT",
        "preset_name TEXT",
        f"source_tier TEXT {_enum_check('source_tier', SourceTier)}",
        "record_count INTEGER",
        "assertion_count INTEGER",
    ])


def _record_table() -> str:
    return _create_table("record", [
        "source_record_uid TEXT PRIMARY KEY",
        "source_id TEXT REFERENCES source(source_id)",
        "line_no INTEGER",
        "input_record_id TEXT",
        "recovery_state TEXT",
        "raw_json TEXT",
    ])


def _entity_table() -> str:
    return _create_table("entity", [
        "entity_id TEXT PRIMARY KEY",
        "label TEXT",
        "entity_type TEXT",
    ])


def _position_table() -> str:
    columns = ["position_id TEXT PRIMARY KEY REFERENCES assertion(row_uid)"]
    columns += [f"{name} {sql_type}" for name, sql_type in POSITION_COLUMNS]
    return _create_table("position", columns)


def _timespan_table() -> str:
    columns = ["timespan_id TEXT PRIMARY KEY REFERENCES assertion(row_uid)"]
    columns += [f"{name} {sql_type}" for name, sql_type in TIMESPAN_COLUMNS]
    return _create_table("timespan", columns)


def _assertion_table() -> str:
    return _create_table("assertion", [
        "row_uid TEXT PRIMARY KEY",
        "source_record_uid TEXT REFERENCES record(source_record_uid)",
        "entity_id TEXT REFERENCES entity(entity_id)",
        "linked_entity_id TEXT REFERENCES entity(entity_id)",
        f"entity_position_link TEXT {_enum_check('entity_position_link', EntityPositionLink)}",
        f"entity_time_link TEXT {_enum_check('entity_time_link', EntityTimeLink)}",
        f"spatial_temporal_link TEXT {_enum_check('spatial_temporal_link', SpatialTemporalLink)}",
        "attribution_origin TEXT",
        # Provenance columns resolved per record (via preset.common), so they are NOT reliably
        # equal to the source-level SourceRow values: raw_source_path defaults to the source
        # path but a preset may map it per record; source_tier likewise; source_label has no
        # design-doc table at all. They live on the assertion so the view projects the exact
        # flat value (byte-parity). input_file_path / preset_id / preset_name ARE source-constant
        # (set unconditionally per record) and stay on the source table.
        "raw_source_path TEXT",
        "source_label TEXT",
        f"source_tier TEXT {_enum_check('source_tier', SourceTier)}",
        f"deleted TEXT {_enum_check('deleted', RecordState)}",
        f"record_type TEXT {_enum_check('record_type', RecordType)}",
        "record_rank INTEGER",
    ])


# --- the assertion_flat view -----------------------------------------------

# Each OUTPUT_COLUMN's SQL expression in the view. The temporal columns carry the ``time_``
# prefix in OUTPUT_COLUMNS but the timespan table stores them unprefixed, so they are
# aliased here; the spatial columns keep their names (position table == Spatial fields).
_SPATIAL_NAMES: frozenset[str] = frozenset(name for name, _ in POSITION_COLUMNS)

_TEMPORAL_VIEW_MAP: dict[str, str] = {
    "time_lower_raw": "t.lower_raw",
    "time_lower_source_field": "t.lower_source_field",
    "time_lower_unix_utc_us": "t.lower_unix_utc_us",
    "time_upper_raw": "t.upper_raw",
    "time_upper_source_field": "t.upper_source_field",
    "time_upper_unix_utc_us": "t.upper_unix_utc_us",
    "utc_offset_hours": "t.utc_offset_hours",
    "time_accuracy_us": "t.time_accuracy_us",
    "temporal_source": "t.temporal_source",
}

# Provenance columns projected from the SOURCE row (source-constant: set unconditionally per
# record, so equal to the SourceRow) and from the RECORD row.
_SOURCE_VIEW_MAP: dict[str, str] = {
    "input_file_path": "s.input_file_path",
    "preset_id": "s.preset_id",
    "preset_name": "s.preset_name",
}
_RECORD_VIEW_MAP: dict[str, str] = {
    "input_record_id": "r.input_record_id",
    "source_record_uid": "r.source_record_uid",
}
# Entity/link columns and the per-assertion columns.
_OTHER_VIEW_MAP: dict[str, str] = {
    "entity": "e_main.label",
    "entity_type": "e_main.entity_type",
    "linked_entity": "e_link.label",
    "entity_position_link": "a.entity_position_link",
    "entity_time_link": "a.entity_time_link",
    "spatial_temporal_link": "a.spatial_temporal_link",
    "row_uid": "a.row_uid",
    "raw_source_path": "a.raw_source_path",
    "source_label": "a.source_label",
    "source_tier": "a.source_tier",
    "deleted": "a.deleted",
    "record_type": "a.record_type",
    "record_rank": "a.record_rank",
}


def _view_expression(column: str) -> str:
    """The ``<table>.<col> AS <output_column>`` projection for one OUTPUT_COLUMN."""
    if column in _SPATIAL_NAMES:
        return f"p.{column} AS {column}"
    if column in _TEMPORAL_VIEW_MAP:
        return f"{_TEMPORAL_VIEW_MAP[column]} AS {column}"
    if column in _SOURCE_VIEW_MAP:
        return f"{_SOURCE_VIEW_MAP[column]} AS {column}"
    if column in _RECORD_VIEW_MAP:
        return f"{_RECORD_VIEW_MAP[column]} AS {column}"
    if column in _OTHER_VIEW_MAP:
        return f"{_OTHER_VIEW_MAP[column]} AS {column}"
    # Every OUTPUT_COLUMN must have a home; an unmapped one is a schema/model desync and
    # would silently drop a CSV column, so fail loudly at import.
    raise AssertionError(f"Invariant violated: OUTPUT_COLUMN {column!r} has no view mapping")


def _assertion_flat_view() -> str:
    projections = ",\n    ".join(_view_expression(column) for column in OUTPUT_COLUMNS)
    # LEFT JOINs throughout so a missing corner (positionless / time-only assertion) or a
    # null entity yields NULLs rather than dropping the row. ORDER BY the assertion rowid
    # reproduces insertion (fan-out) order, which the flat frame's row order depends on.
    return (
        "CREATE VIEW assertion_flat AS\nSELECT\n    "
        + projections
        + "\nFROM assertion a\n"
        "LEFT JOIN record r ON a.source_record_uid = r.source_record_uid\n"
        "LEFT JOIN source s ON r.source_id = s.source_id\n"
        "LEFT JOIN entity e_main ON a.entity_id = e_main.entity_id\n"
        "LEFT JOIN entity e_link ON a.linked_entity_id = e_link.entity_id\n"
        "LEFT JOIN position p ON p.position_id = a.row_uid\n"
        "LEFT JOIN timespan t ON t.timespan_id = a.row_uid\n"
        "ORDER BY a.rowid;"
    )


def schema_statements() -> list[str]:
    """The ordered DDL statements that build the whole run schema."""
    return [
        _run_table(),
        _source_table(),
        _record_table(),
        _entity_table(),
        _position_table(),
        _timespan_table(),
        _assertion_table(),
        _assertion_flat_view(),
    ]


def create_schema(connection: sqlite3.Connection) -> None:
    """Materialise every table and the assertion_flat view on ``connection``."""
    for statement in schema_statements():
        connection.execute(statement)
    connection.commit()
