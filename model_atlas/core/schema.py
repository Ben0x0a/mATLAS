"""The run-database DDL, derived one-directionally from ``core/vocabularies.py``.

Defines:    ``schema_statements()`` (the ordered CREATE TABLE / INDEX / VIEW strings for
            the 21 evidence tables + ``matlas_meta`` + the two finding VIEWS) and
            ``create_schema(conn)`` which applies them and stamps the versioning pragmas
            (``foreign_keys``, ``user_version``, ``application_id``) and the one
            ``matlas_meta`` row. Also exposes ``SCHEMA_VERSION`` / ``FAMILIES_VERSION`` /
            ``APPLICATION_ID`` / ``USER_VERSION`` and ``COPYABLE_TABLES`` (ingest order).
Used by:    the store (T13 — creates the schema then writes rows in ingest order),
            tests.core.test_schema, and any tool that needs a fresh run DB.
Depends on: ``model_atlas.core.vocabularies`` (the frozen table dataclasses give the
            columns + their python types; the closed enums give the CHECK constraints)
            and the standard library only (``sqlite3``, ``dataclasses``, ``enum``,
            ``typing``). No ORM: the derivation is one-way, vocabularies -> DDL.

Derivation discipline (why the table list is NOT a wall of hand-written column strings):
each column's NAME and TYPE derive from the matching frozen dataclass in
``vocabularies.py`` — ``str`` -> TEXT, ``int`` -> INTEGER, ``float`` -> REAL, a genuine
multi-type union (``float | str``) -> TEXT — so adding a field to a family adds the
column, and no type is restated. Each column's CHECK constraint derives from the same
enum the model annotates it with, so an invalid vocabulary value is impossible to store.
What the dataclasses do NOT carry — the relational STRUCTURE (primary keys, which FK a
``*_id`` points at, the natural-key UNIQUE tuples, the indexes) and the ``matlas_meta``
row (a meta table, not a family) — is declared in one small explicit ``_TABLES``
mapping below, beside the
dataclass it augments. That structure genuinely does not exist in the model, so encoding
it once here (rather than duplicating column names as strings) is the minimal honest
source of truth; the column names it references are asserted against the dataclass at
import so the two can never silently drift (see ``_build_table``).
"""
from __future__ import annotations

import sqlite3
import typing
from dataclasses import fields as dataclass_fields
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin, get_type_hints

from model_atlas.core import vocabularies as V

# --- versioning + self-ingestion stamps (R16/R19) -------------------------
# schema_version / families_version go into the single matlas_meta row (R19). user_version
# is SQLite's per-file integer; we set it to the numeric schema generation so a tool can
# gate on `PRAGMA user_version` without opening a table. application_id is the four-byte
# self-ingestion stamp (R16): discovery reads it to refuse/flag a mATLAS run DB fed back
# in as evidence. The value 0x6D41_744C spells the ASCII bytes "mAtL" (mATLAS) — an
# arbitrary but memorable, documented constant, chosen once and never reused elsewhere.
SCHEMA_VERSION = "1"
FAMILIES_VERSION = "1"
USER_VERSION = 1
APPLICATION_ID = 0x6D41_744C  # b"mAtL" — the mATLAS run-DB self-ingestion stamp (R16)

# The normalisation / stemming rule-set versions recorded once per run (decision 17/22).
# Placeholders at schema-creation time; the pipeline overwrites the matlas_meta row with
# the real rule-set versions it applied. Kept here so a freshly created DB is never
# missing its single meta row.
_DEFAULT_NORMALISATION_RULESET = "norm_rules/1.0"
_DEFAULT_STEMMING_RULESET = "stem_rules/1.0"


# --- type derivation from the dataclass annotations ------------------------

def _sql_type(annotation: Any) -> str:
    """Map a family-dataclass field annotation to a SQLite column type.

    HOW: ``str`` -> TEXT, ``int`` -> INTEGER, ``float`` -> REAL; an enum member type is a
    ``str``-Enum so also TEXT; ``T | None`` maps by ``T``; a genuine multi-type union
    (``float | str``, ``int | float``) or anything unrecognised falls back to TEXT.
    WHY: the model column's storage class is a property of the model, declared once on the
    dataclass field — a preset never restates it and the DDL never contradicts it.
    """
    origin = get_origin(annotation)
    if origin is typing.Union or origin is UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
    else:
        non_none = [annotation]
    if len(non_none) == 1:
        only = non_none[0]
        if isinstance(only, type) and issubclass(only, Enum):
            return "TEXT"          # every vocabulary enum is a str-Enum -> stored as text
        if only is str:
            return "TEXT"
        if only is float:
            return "REAL"
        if only is bool:
            return "INTEGER"
        if only is int:
            return "INTEGER"
    # A genuine multi-type union (utc_offset_hours_* is float|str) or typing.Any: keep it
    # as free TEXT so no value is rejected.
    return "TEXT"


def _enum_of(annotation: Any) -> type[Enum] | None:
    """The str-Enum a field is annotated with (through an Optional), or None.

    Used to attach a CHECK: a column typed by a closed vocabulary gets a membership
    constraint derived from that same enum, so an out-of-vocabulary value cannot be stored.
    """
    origin = get_origin(annotation)
    candidates = (get_args(annotation) if origin in (typing.Union, UnionType)
                  else (annotation,))
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, Enum):
            return candidate
    return None


def _enum_check(column: str, enum_type: type[Enum]) -> str:
    """A ``CHECK (col IN (...) OR col IS NULL)`` clause from an enum's values.

    NULL is always allowed because every vocabulary column in the model is Optional (an
    unmapped edge/role is None); the constraint enforces the closed set but must not reject
    the legal absence of a value.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_type)
    return f"CHECK ({column} IN ({values}) OR {column} IS NULL)"


def _hints(dataclass_type: type) -> dict[str, Any]:
    """Field-name -> resolved annotation for a ``from __future__``-annotated dataclass."""
    resolved = get_type_hints(dataclass_type)
    return {f.name: resolved[f.name] for f in dataclass_fields(dataclass_type)}


# --- the explicit relational structure (what the dataclasses cannot carry) -
# One entry per physical table. ``dataclass`` gives the columns + their python types
# (single source of truth for names & storage classes); the rest is pure relational
# structure that has no home in a frozen data container:
#   pk        — the PRIMARY KEY column.
#   not_null  — columns to mark NOT NULL beyond the PK (FKs the doc marks NOT NULL, etc.).
#   fks       — column -> (target_table, target_column): emitted as REFERENCES clauses.
#   unique_cols — single columns to mark UNIQUE inline (the three owned-corner FKs, the
#               entity FK, archive.path…).
#   unique    — composite UNIQUE(tuple) natural keys (§2a and the link tables).
#   extra_columns — DDL-only columns absent from the dataclass, as (name, "TYPE …").
#               Currently unused (the one-time entity.raw_identifier gap was closed in
#               vocabularies.py); kept because a future meta-ish column may need it.
#   indexes   — (index_name, (col, …)) non-unique lookup indexes (SQLite makes none for FKs).
# The column NAMES referenced here are checked against the dataclass at build time, so a
# rename on either side raises instead of silently dropping a constraint (see _build_table).

@typing.no_type_check
def _table_specs() -> "list[dict[str, Any]]":
    """The ordered table specifications; ingest/create order (parents before children)."""
    return [
        # -- Layer 0: acquisition --------------------------------------------
        {"name": "case_metadata", "dataclass": V.CaseMetadata, "pk": "case_id",
         "not_null": ["case_number"]},
        {"name": "run", "dataclass": V.Run, "pk": "run_id",
         "not_null": ["case_id"],
         "fks": {"case_id": ("case_metadata", "case_id")}},
        {"name": "archive", "dataclass": V.Archive, "pk": "archive_id",
         "not_null": ["path", "cd_sha256"], "unique_cols": ["path"],
         "indexes": [("ix_archive_cd", ("cd_sha256",))]},
        {"name": "run_archive", "dataclass": V.RunArchive, "pk": "run_archive_id",
         "not_null": ["run_id", "archive_id", "skipped"],
         "fks": {"run_id": ("run", "run_id"),
                 "archive_id": ("archive", "archive_id")},
         "unique": [("run_id", "archive_id")]},
        {"name": "source", "dataclass": V.Source, "pk": "source_id",
         "not_null": ["run_id", "input_file_path"],
         "fks": {"run_id": ("run", "run_id")},
         "enum_checks": {"source_tier": V.SourceTier},
         "unique": [("run_id", "input_file_path", "inner_path", "subunit")],
         "indexes": [("ix_source_fp", ("fingerprint_sha256",))]},
        {"name": "preset", "dataclass": V.Preset, "pk": "preset_id",
         "not_null": ["key", "definition_digest"],
         "unique": [("key", "version")]},
        {"name": "run_preset", "dataclass": V.RunPreset, "pk": "run_preset_id",
         "not_null": ["run_id", "preset_id"],
         "fks": {"run_id": ("run", "run_id"),
                 "preset_id": ("preset", "preset_id")},
         "unique": [("run_id", "preset_id")]},
        {"name": "raw_record", "dataclass": V.RawRecord, "pk": "raw_record_id",
         "not_null": ["source_id", "line_no", "raw_json"],
         "fks": {"source_id": ("source", "source_id")},
         "enum_checks": {"recovery_state": V.RecordState,
                         "recovery_source": V.RecoverySource},
         "unique": [("source_id", "line_no")]},
        # -- Layer 1: assertion ----------------------------------------------
        {"name": "activity", "dataclass": V.Activity, "pk": "activity_id",
         "enum_checks": {"kind": V.ActivityKind}},
        {"name": "activity_record", "dataclass": V.ActivityRecord,
         "pk": "activity_record_id",
         "not_null": ["activity_id", "raw_record_id"],
         "fks": {"activity_id": ("activity", "activity_id"),
                 "raw_record_id": ("raw_record", "raw_record_id")},
         "unique": [("activity_id", "raw_record_id")]},
        {"name": "entity", "dataclass": V.Entity, "pk": "entity_id",
         "not_null": ["identifier_type", "type"],
         # identifier_type carries NO enum CHECK: it is a free string (a preset may name any
         # kind; IdentifierType only lists the ones the normaliser knows). NOT NULL still
         # holds — every entity states a kind, defaulting to 'none'.
         "enum_checks": {"type": V.EntityType, "concerns_origin": V.Origin},
         "indexes": [("ix_entity_norm", ("identifier_type", "identifier")),
                     ("ix_entity_concerns", ("concerns_uid",))]},
        {"name": "spatial_expression", "dataclass": V.SpatialExpression,
         "pk": "spatial_expression_id", "not_null": ["referential"],
         "enum_checks": {"referential": V.SpatialReferential}},
        {"name": "spatial_component", "dataclass": V.SpatialComponent,
         "pk": "spatial_component_id",
         "not_null": ["spatial_expression_id", "ordinal", "part"],
         "fks": {"spatial_expression_id":
                 ("spatial_expression", "spatial_expression_id")},
         "enum_checks": {"spatial_role": V.SpatialRole},
         "unique": [("spatial_expression_id", "ordinal")]},
        {"name": "temporal_expression", "dataclass": V.TemporalExpression,
         "pk": "temporal_expression_id", "not_null": ["kind"],
         "enum_checks": {"kind": V.TemporalKind, "referential": V.TemporalReferential}},
        {"name": "temporal_component", "dataclass": V.TemporalComponent,
         "pk": "temporal_component_id",
         "not_null": ["temporal_expression_id", "ordinal", "part"],
         "fks": {"temporal_expression_id":
                 ("temporal_expression", "temporal_expression_id")},
         "enum_checks": {"temporal_role": V.TemporalRole},
         "unique": [("temporal_expression_id", "ordinal")]},
        {"name": "assertion", "dataclass": V.Assertion, "pk": "assertion_id",
         "not_null": ["raw_record_id", "preset_id", "output_ordinal", "entity_id",
                      "content_hash", "claim_digest"],
         "fks": {"raw_record_id": ("raw_record", "raw_record_id"),
                 "preset_id": ("preset", "preset_id"),
                 "entity_id": ("entity", "entity_id"),
                 "activity_id": ("activity", "activity_id"),
                 "spatial_expression_id":
                 ("spatial_expression", "spatial_expression_id"),
                 "temporal_expression_id":
                 ("temporal_expression", "temporal_expression_id")},
         # The owned corners + the entity are 1:1 with the assertion (§2 guard: two
         # assertions may never share a corner row or an entity row).
         "unique_cols": ["entity_id", "spatial_expression_id", "temporal_expression_id"],
         "enum_checks": {"entity_origin": V.Origin, "entity_role": V.EntityRole,
                         "entity_position_link": V.EntityPositionLink,
                         "entity_time_link": V.EntityTimeLink,
                         "spatial_temporal_link": V.SpatialTemporalLink,
                         "deleted": V.RecordState},
         "unique": [("raw_record_id", "preset_id", "output_ordinal")],
         "indexes": [("ix_assertion_ch", ("content_hash",)),
                     ("ix_assertion_cd", ("claim_digest",)),
                     ("ix_assertion_act", ("activity_id",))]},
        # -- Layer 2: integration --------------------------------------------
        {"name": "spatial_integration", "dataclass": V.SpatialIntegration,
         "pk": "spatial_integration_id",
         "not_null": ["spatial_expression_id", "method"],
         "fks": {"spatial_expression_id":
                 ("spatial_expression", "spatial_expression_id")},
         "unique_cols": ["spatial_expression_id"]},   # v1 single integration (D30)
        {"name": "spatial_index", "dataclass": V.SpatialIndex, "pk": "spatial_index_id",
         "not_null": ["spatial_integration_id"],
         "fks": {"spatial_integration_id":
                 ("spatial_integration", "spatial_integration_id")},
         "unique_cols": ["spatial_integration_id"],
         "indexes": [("ix_spatial_index_cell", ("s2_cell_id",))]},
        {"name": "timespan_integration", "dataclass": V.TimespanIntegration,
         "pk": "timespan_integration_id",
         "not_null": ["temporal_expression_id", "method"],
         "fks": {"temporal_expression_id":
                 ("temporal_expression", "temporal_expression_id")},
         "unique_cols": ["temporal_expression_id"]},
        # -- Layer 3: analysis -----------------------------------------------
        {"name": "analysis", "dataclass": V.Analysis, "pk": "analysis_id",
         "not_null": ["assertion_id", "method"],
         "fks": {"assertion_id": ("assertion", "assertion_id")},
         "unique": [("assertion_id", "method")]},
        {"name": "edit_log", "dataclass": V.EditLog, "pk": "edit_id",
         "not_null": ["ordinal"],
         "enum_checks": {"edit_kind": V.EditKind},
         "unique_cols": ["ordinal"]},   # D41: a monotonic UNIQUE journal position
    ]


# ``matlas_meta`` is a DDL-only table (no family — it is per-DB metadata, not evidence),
# so it is declared explicitly rather than derived. One row, written by create_schema.
_MATLAS_META_DDL = (
    "CREATE TABLE matlas_meta (\n"
    "    schema_version TEXT,\n"
    "    families_version TEXT,\n"
    "    normalisation_ruleset TEXT,\n"
    "    stemming_ruleset TEXT\n"
    ");"
)

# The two finding VIEWS (schema §Silent-loss inventory): duplicate evidence is always a
# QUERY that SURFACES, never an ingest-time merge. Byte-for-byte the demo/doc definitions.
_DUPLICATE_SOURCES_VIEW = (
    "CREATE VIEW duplicate_sources AS\n"
    "    SELECT fingerprint_sha256, COUNT(*) AS copies,\n"
    "           GROUP_CONCAT(input_file_path, ' | ') AS files\n"
    "    FROM source GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;"
)
_POSSIBLE_SAME_EVIDENCE_VIEW = (
    "CREATE VIEW possible_same_evidence AS\n"
    "    SELECT a.claim_digest, COUNT(*) AS copies,\n"
    "           GROUP_CONCAT(s.input_file_path, ' | ') AS files\n"
    "    FROM assertion a\n"
    "    JOIN raw_record r ON r.raw_record_id = a.raw_record_id\n"
    "    JOIN source s ON s.source_id = r.source_id\n"
    "    GROUP BY a.claim_digest HAVING COUNT(*) > 1;"
)


# --- table builder ---------------------------------------------------------

def _column_ddl(name: str, sql_type: str, spec: dict[str, Any]) -> str:
    """The full column clause: ``name TYPE [PK] [NOT NULL] [UNIQUE] [REFERENCES] [CHECK]``."""
    parts = [name, sql_type]
    if name == spec["pk"]:
        parts.append("PRIMARY KEY")
    if name in spec.get("not_null", ()):
        parts.append("NOT NULL")
    if name in spec.get("unique_cols", ()):
        parts.append("UNIQUE")
    fk = spec.get("fks", {}).get(name)
    if fk is not None:
        parts.append(f"REFERENCES {fk[0]}({fk[1]})")
    enum_type = spec.get("enum_checks", {}).get(name)
    if enum_type is not None:
        parts.append(_enum_check(name, enum_type))
    return " ".join(parts)


def _build_table(spec: dict[str, Any]) -> str:
    """The CREATE TABLE statement for one spec, columns derived from its dataclass.

    HOW: the dataclass fields give the (name, type) columns in declaration order; any
    ``extra_columns`` are spliced in and the whole is re-ordered by ``column_order`` when
    the physical column order must differ from the dataclass (entity.raw_identifier). Every
    structural key referenced by the spec (pk, not_null, fks, unique, enum_checks, indexes)
    is checked to name a real column, so a rename on either side raises here rather than
    silently emitting a table missing a constraint.
    """
    hints = _hints(spec["dataclass"])
    columns: dict[str, str] = {name: _sql_type(tp) for name, tp in hints.items()}
    for extra_name, extra_type in spec.get("extra_columns", []):
        columns[extra_name] = extra_type
    order = spec.get("column_order", list(columns.keys()))
    # Invariant: column_order must be a permutation of the real columns — a typo here would
    # silently drop or invent a column, exactly the class of loss this schema guards against.
    assert set(order) == set(columns), (
        f"column_order for {spec['name']} does not match its columns: "
        f"{set(order) ^ set(columns)}")
    _assert_columns_exist(spec, set(columns))

    column_lines = [_column_ddl(name, columns[name], spec) for name in order]
    for unique_cols in spec.get("unique", []):
        column_lines.append(f"UNIQUE ({', '.join(unique_cols)})")
    body = ",\n    ".join(column_lines)
    return f"CREATE TABLE {spec['name']} (\n    {body}\n);"


def _assert_columns_exist(spec: dict[str, Any], columns: set[str]) -> None:
    """Fail loudly if the spec references a column the table does not have.

    WHY: the explicit structure names columns by string; if a family field is renamed but
    the spec is not (or vice-versa), a constraint would silently vanish — the precise
    silent-loss failure this whole model is built to make impossible.
    """
    referenced: set[str] = {spec["pk"]}
    referenced |= set(spec.get("not_null", ()))
    referenced |= set(spec.get("unique_cols", ()))
    referenced |= set(spec.get("fks", {}))
    referenced |= set(spec.get("enum_checks", {}))
    for unique_cols in spec.get("unique", []):
        referenced |= set(unique_cols)
    for _, index_cols in spec.get("indexes", []):
        referenced |= set(index_cols)
    missing = referenced - columns
    assert not missing, f"table {spec['name']!r} spec references unknown columns: {missing}"


def _index_ddl(table: str, index_name: str, index_cols: tuple[str, ...]) -> str:
    return f"CREATE INDEX {index_name} ON {table}({', '.join(index_cols)});"


# --- public API ------------------------------------------------------------

# The tables copyable row-for-row from one run DB to another, in FK-safe (ingest) order.
# matlas_meta is metadata (one row per DB, written by create_schema), not evidence, so it
# is excluded — the store and the demo-load test iterate this list, parents first.
COPYABLE_TABLES: tuple[str, ...] = tuple(spec["name"] for spec in _table_specs())


def schema_statements() -> list[str]:
    """The ordered DDL statements building the whole run schema (deterministic).

    Order: matlas_meta first, then every table parent-before-child, each table's indexes
    immediately after it, then the two finding VIEWS. Deterministic because ``_table_specs``
    is a fixed list and dataclass fields enumerate in declaration order — so the golden
    snapshot is stable.
    """
    statements: list[str] = [_MATLAS_META_DDL]
    for spec in _table_specs():
        statements.append(_build_table(spec))
        for index_name, index_cols in spec.get("indexes", []):
            statements.append(_index_ddl(spec["name"], index_name, index_cols))
    statements.append(_DUPLICATE_SOURCES_VIEW)
    statements.append(_POSSIBLE_SAME_EVIDENCE_VIEW)
    return statements


def create_schema(connection: sqlite3.Connection) -> None:
    """Materialise the full schema on ``connection`` and stamp its versioning metadata.

    HOW: enable FK enforcement, set user_version + application_id, run every DDL statement,
    then write the single matlas_meta row. WHY the pragmas: application_id is the R16
    self-ingestion stamp so discovery can refuse a mATLAS DB fed back as evidence;
    user_version lets a tool gate on the schema generation without opening a table.
    """
    # FK enforcement is per-connection in SQLite and OFF by default; the whole model relies
    # on it (a dangling corner FK must raise at ingest), so it is turned on before any write.
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.execute(f"PRAGMA user_version = {USER_VERSION};")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID};")
    for statement in schema_statements():
        connection.execute(statement)
    connection.execute(
        "INSERT INTO matlas_meta "
        "(schema_version, families_version, normalisation_ruleset, stemming_ruleset) "
        "VALUES (?, ?, ?, ?);",
        (SCHEMA_VERSION, FAMILIES_VERSION,
         _DEFAULT_NORMALISATION_RULESET, _DEFAULT_STEMMING_RULESET))
    connection.commit()
