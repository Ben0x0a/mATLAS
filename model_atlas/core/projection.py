"""The flat CSV as a GENERATED projection query over the run database.

Defines:    ``CANONICAL_COLUMNS`` (the ordered flat-CSV column contract encoded from
            [docs/csv_migration.md](../../docs/csv_migration.md)), ``build_projection_sql``
            (assembles the per-run SELECT — canonical block first, then the exploded
            ``orig_<col>`` passthrough), and ``project`` (runs it against a read-only
            connection and returns the flat pandas frame with numeric dtypes restored
            and the derived ``locates`` filled).
Used by:    the CSV exporter (T50) and any read-only consumer wanting the flat view;
            tests.core.test_projection.
Depends on: ``model_atlas.core.vocabularies`` (``EntityPositionLink.locates`` — the single
            source of the ``locates`` derivation), ``pandas`` (the frame + numeric
            re-coercion the old frame builder did), and the standard library
            (``sqlite3``, ``json``).

Why a GENERATED query and not a static VIEW (R21)
-------------------------------------------------
The canonical columns are fixed, but the ``orig_<col>`` verbatim passthrough is the
UNION of every raw record's own keys — a set that differs per source and therefore per
run. A static VIEW cannot carry a per-run column set, so the projection is assembled
as a SQL string once per run: the canonical SELECT list is constant, the ``orig_``
tail is computed from the records actually present.

One row PER ASSERTION (decisions 14/26)
---------------------------------------
An assertion exists only per placement, so the line count IS the placement count (a
one-legged CDR is one line, a 20-recipient message is one). The owned corners
(entity is mandatory; the spatial/temporal expressions are optional) are LEFT JOINed,
so a corner-less assertion yields EMPTY coordinate/time cells — never dropped, never
invented (the §2 "positionless claim vanishes" guard).

Column-contract decisions (recorded where csv_migration.md is a mapping, not a literal
column list)
------------------------------------------------------------------------------------
- Order = the migration's "Every old column, mapped" block first (for reviewer
  diffing), then the "New columns" block, then ``orig_<col>`` (its regeneration
  procedure).
- ``position_type`` -> ``spatial_role`` and ``position_source`` -> ``spatial_source``
  (cross-check findings F3 / row 42: the settled model's names; the old headers are
  retired).
- ``utc_offset_hours`` -> ``utc_offset_hours_lower`` / ``_upper`` (finding F2).
- ``deleted`` sources from ``assertion.deleted`` (the app's own flag, migration row 65
  / R11); the separate ``recovery_state`` new column sources from
  ``raw_record.recovery_state``.
- ``locates`` is SELECTed as a ``NULL`` placeholder and filled in Python from
  ``EntityPositionLink(...).locates`` — the mapping is declared once in vocabularies,
  so it is never re-encoded as a SQL ``CASE`` (decision 11).
- ``counterpart`` / ``facets`` / ``context`` are OPTIONAL bonus columns (decision 35):
  best-effort ``json_extract`` over ``activity.context_json``. No MANDATORY column
  sources from the activity table.
- The preferred-integration predicate is future-proofing only: v1 is single-integration
  (``UNIQUE(expression_id)``), so there is NO ``preferred`` column to filter on — a plain
  LEFT JOIN already yields the one row. When plural candidates arrive, add
  ``AND <integration>.preferred = 1`` to those two joins (see the marked comments).
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

from model_atlas.core import vocabularies as V

# The passthrough column prefix; a source field ``a_party`` becomes ``orig_a_party``.
PASSTHROUGH_PREFIX = "orig_"

# --- the canonical flat-CSV column contract (docs/csv_migration.md) ----------
# Each entry is (output column name, SQL expression). The order IS the contract; the
# expression names the source table.column (or a component subquery / json_extract).
# Table aliases: a=assertion, r=raw_record, s=source, rn=run, c=case_metadata,
# e=entity, p=preset, sx/si/six=spatial expression/integration/index,
# tx/ti=temporal expression/timespan integration, act=activity, an=analysis.

# A standard temporal/spatial component is reassembled by its ordinal-ordered, standard
# part name; ``is_standard_part`` in vocabularies is the authoritative predicate, and the
# demo only ever writes standard parts, so filtering on the literal part name here selects
# exactly the export-projected component (LIMIT 1 keeps it deterministic if a malformed
# expression ever carried two).
def _temporal_part(part: str, column: str) -> str:
    """Correlated subquery picking one temporal component's ``column`` for ``part``."""
    return (f"(SELECT tc.{column} FROM temporal_component tc "
            f"WHERE tc.temporal_expression_id = a.temporal_expression_id "
            f"AND tc.part = '{part}' ORDER BY tc.ordinal LIMIT 1)")


def _spatial_part(part: str, column: str) -> str:
    """Correlated subquery picking one spatial component's ``column`` for ``part``."""
    return (f"(SELECT sc.{column} FROM spatial_component sc "
            f"WHERE sc.spatial_expression_id = a.spatial_expression_id "
            f"AND sc.part = '{part}' ORDER BY sc.ordinal LIMIT 1)")


# The place's role (old ``position_type``, F3): the first non-null component role by
# ordinal — carried on the raw_position component for cell/camera/address, absent (NULL)
# for a bare lat/lon fix, exactly as the old single position_type column behaved.
_SPATIAL_ROLE = (
    "(SELECT sc.spatial_role FROM spatial_component sc "
    "WHERE sc.spatial_expression_id = a.spatial_expression_id "
    "AND sc.spatial_role IS NOT NULL ORDER BY sc.ordinal LIMIT 1)")

# raw_source_path (decision 24): the data-source where the data originally lived, falling
# back to the chain-inclusive input source path (``input_file_path!inner_path``).
_RAW_SOURCE_PATH = (
    "COALESCE(s.data_source, CASE WHEN s.inner_path IS NOT NULL "
    "THEN s.input_file_path || '!' || s.inner_path ELSE s.input_file_path END)")

_CANONICAL_SELECT: tuple[tuple[str, str], ...] = (
    # -- Block 1: every old column, mapped (csv_migration.md, old CSV order) ----
    ("entity", "e.identifier"),
    ("entity_type", "e.type"),
    ("linked_entity", "e.concerns"),
    ("time_lower_raw", _temporal_part("lower", "raw_value")),
    ("time_lower_source_field", _temporal_part("lower", "source_field")),
    ("time_lower_unix_utc_us", "ti.lower_unix_utc_us"),
    ("time_upper_raw", _temporal_part("upper", "raw_value")),
    ("time_upper_source_field", _temporal_part("upper", "source_field")),
    ("time_upper_unix_utc_us", "ti.upper_unix_utc_us"),
    ("utc_offset_hours_lower", "ti.utc_offset_hours_lower"),
    ("utc_offset_hours_upper", "ti.utc_offset_hours_upper"),
    ("time_accuracy_us", "ti.time_accuracy_us"),
    ("temporal_source", "tx.temporal_source"),
    ("latitude_wgs84", "si.latitude_wgs84"),
    ("latitude_source_field", _spatial_part("latitude_wgs84", "source_field")),
    ("longitude_wgs84", "si.longitude_wgs84"),
    ("longitude_source_field", _spatial_part("longitude_wgs84", "source_field")),
    ("altitude_m", "si.altitude_m"),
    ("spatial_role", _SPATIAL_ROLE),                    # old position_type (F3)
    ("raw_position", _spatial_part("raw_position", "raw_value")),
    ("spatial_source", "sx.spatial_source"),            # old position_source
    ("horizontal_accuracy_m", "si.horizontal_accuracy_m"),
    ("vertical_accuracy_m", "si.vertical_accuracy_m"),
    ("horizontal_speed_kmh", "si.horizontal_speed_kmh"),
    ("vertical_speed_kmh", "si.vertical_speed_kmh"),
    ("horizontal_speed_accuracy_kmh", "si.horizontal_speed_accuracy_kmh"),
    ("vertical_speed_accuracy_kmh", "si.vertical_speed_accuracy_kmh"),
    ("heading_deg", "si.heading_deg"),
    ("heading_accuracy_deg", "si.heading_accuracy_deg"),
    ("beam_azimuth_deg", "si.beam_azimuth_deg"),
    ("beam_width_deg", "si.beam_width_deg"),
    ("entity_position_link", "a.entity_position_link"),
    ("entity_time_link", "a.entity_time_link"),
    ("spatial_temporal_link", "a.spatial_temporal_link"),
    ("raw_source_path", _RAW_SOURCE_PATH),
    ("input_file_path", "s.input_file_path"),
    ("input_record_id", "a.input_record_id"),
    ("source_record_uid", "r.raw_record_id"),
    ("row_uid", "a.assertion_id"),
    ("preset_id", "p.key"),                             # the preset KEY (migration row 61)
    ("preset_name", "p.name"),
    ("source_label", "a.source_label"),
    ("source_tier", "s.source_tier"),
    ("deleted", "a.deleted"),                           # the app's own flag (R11)
    ("record_type", "an.record_type"),
    ("record_rank", "an.record_rank"),
    # -- Block 2: new columns (csv_migration.md order) -------------------------
    ("case_number", "c.case_number"),
    ("content_uid", "a.content_hash"),
    ("claim_digest", "a.claim_digest"),
    ("raw_identifier", "e.raw_identifier"),
    ("identifier_type", "e.identifier_type"),
    ("entity_role", "a.entity_role"),
    ("concerns_origin", "e.concerns_origin"),
    ("spatial_referential", "sx.referential"),
    ("locates", "NULL"),                                # derived in Python (decision 11)
    ("spatial_method", "si.method"),
    ("temporal_method", "ti.method"),
    ("time_lower_stated_offset", _temporal_part("lower", "stated_offset")),
    ("time_upper_stated_offset", _temporal_part("upper", "stated_offset")),
    ("time_lower_datetime_iso", "ti.lower_datetime_iso"),
    ("time_upper_datetime_iso", "ti.upper_datetime_iso"),
    ("time_lower_date", "ti.lower_date"),
    ("time_upper_date", "ti.upper_date"),
    ("time_lower_time", "ti.lower_time"),
    ("time_upper_time", "ti.upper_time"),
    ("recovery_state", "r.recovery_state"),
    ("data_source", "s.data_source"),
    ("activity_id", "a.activity_id"),
    ("activity_kind", "act.kind"),                      # bonus, optional (decision 35)
    ("counterpart", "json_extract(act.context_json, '$.counterpart')"),
    ("facets", "json_extract(act.context_json, '$.facets')"),
    ("context", "json_extract(act.context_json, '$.context')"),
    ("s2_cell_id", "six.s2_cell_id"),
)

# The ordered flat-CSV column names — the encoded csv_migration.md contract. The exporter
# and the tests assert the projected frame's canonical block matches this exactly.
CANONICAL_COLUMNS: tuple[str, ...] = tuple(name for name, _ in _CANONICAL_SELECT)

# Columns whose MODEL type is a numeric union (``float | str`` / ``int | float``) so the
# schema stores them as TEXT, but whose values are semantically numeric. After reading the
# frame back their dtype is restored so the CSV renders them unquoted exactly as the old
# dict-built frame did — UNLESS a non-numeric sentinel (``UTC_OFFSET_UNKNOWN`` = "N/A") is
# present, in which case the column stays text (quoted), keeping "value, zone unknown"
# visibly distinct from a real offset.
_NUMERIC_TEXT_COLUMNS: tuple[str, ...] = (
    "utc_offset_hours_lower", "utc_offset_hours_upper", "time_accuracy_us",
)

# INTEGER-typed projection columns that may carry NULLs (a corner-less assertion). Read
# as pandas nullable ``Int64`` rather than the float64 pandas would otherwise infer:
# ``s2_cell_id`` is a signed int64 S2 leaf cell whose value EXCEEDS float64's 53-bit
# integer range — a float round-trip would silently corrupt the cell id (a §2-class
# silent loss) — and the epoch/date/time columns render as clean unquoted integers.
_INTEGER_COLUMNS: tuple[str, ...] = (
    "time_lower_unix_utc_us", "time_upper_unix_utc_us",
    "time_lower_date", "time_upper_date", "time_lower_time", "time_upper_time",
    "record_rank", "s2_cell_id",
)

# The join chain up the grain (assertion -> raw_record -> source -> run -> case) plus the
# owned corners, their integrations, the S2 index and the current analysis method. The
# ``?`` binds the resolved ranking method (see build_projection_sql). ORDER BY a.rowid
# reproduces ingest order, so the flat frame is deterministic.
_FROM_JOINS = """
FROM assertion a
JOIN raw_record r ON r.raw_record_id = a.raw_record_id
JOIN source s ON s.source_id = r.source_id
JOIN run rn ON rn.run_id = s.run_id
JOIN case_metadata c ON c.case_id = rn.case_id
JOIN entity e ON e.entity_id = a.entity_id
JOIN preset p ON p.preset_id = a.preset_id
LEFT JOIN spatial_expression sx ON sx.spatial_expression_id = a.spatial_expression_id
LEFT JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id
LEFT JOIN spatial_index six ON six.spatial_integration_id = si.spatial_integration_id
LEFT JOIN temporal_expression tx ON tx.temporal_expression_id = a.temporal_expression_id
LEFT JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id
LEFT JOIN activity act ON act.activity_id = a.activity_id
LEFT JOIN analysis an ON an.assertion_id = a.assertion_id AND an.method = ?
ORDER BY a.rowid
""".strip()
# NOTE (future-proofing, R21/decision 30): when plural integration candidates arrive, add
# ``AND si.preferred = 1`` / ``AND ti.preferred = 1`` to the two integration joins above.
# v1 is single-integration (UNIQUE(expression_id)), so no ``preferred`` column exists yet
# and a plain LEFT JOIN already yields the one preferred row.


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier, escaping embedded double-quotes.

    WHY: an ``orig_<col>`` alias derives from a source field name that may contain spaces,
    dots or other characters a bare identifier cannot carry; quoting keeps the generated
    SELECT valid whatever the source called its column.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _passthrough_names(source_fields: list[str], prefix: str) -> dict[str, str]:
    """Map each source field to ``<prefix><field>``, preserving first-appearance order.

    A name that would collide with a canonical column is disambiguated with a trailing
    ``_`` (the same rule the old frame builder used). The mapping is a pure function of the
    field name, so it is identical across sources and the merge stays stable.
    """
    used = set(CANONICAL_COLUMNS)
    names: dict[str, str] = {}
    for field in source_fields:
        name = f"{prefix}{field}"
        # A collision with a canonical column would silently overwrite it in the frame;
        # the trailing ``_`` keeps the verbatim passthrough and the canonical column both.
        while name in used:
            name += "_"
        names[field] = name
        used.add(name)
    return names


def _orig_fields_in_first_appearance_order(connection: sqlite3.Connection) -> list[str]:
    """Union of every raw record's ``raw_json`` keys, in first-appearance order.

    HOW: records enumerate in ``rowid`` (ingest) order — the determinism-rule-1 order — and
    each record's keys are appended the first time they are seen. Dicts preserve insertion
    order, so a run spanning several sources with differing schemas yields a stable,
    reproducible passthrough column set (a row from a source lacking a key is simply blank).
    """
    seen: set[str] = set()
    fields: list[str] = []
    for (raw_json,) in connection.execute(
            "SELECT raw_json FROM raw_record ORDER BY rowid"):
        for key in json.loads(raw_json):
            if key not in seen:
                seen.add(key)
                fields.append(key)
    return fields


def _resolve_ranking_method(
        connection: sqlite3.Connection, ranking_method: str | None) -> str | None:
    """The analysis method the CSV projects, or None when there is no analysis.

    WHY MAX when unspecified: re-running a changed ranker ADDS rows (decision 19), method
    strings are versioned (``untangle/2.0``), so the lexicographic maximum is the latest —
    the "current" verdict. The caller may pin a specific method explicitly.
    """
    if ranking_method is not None:
        return ranking_method
    row = connection.execute("SELECT MAX(method) FROM analysis").fetchone()
    return row[0] if row is not None else None


def build_projection_sql(
    connection: sqlite3.Connection,
    *,
    include_source_columns: bool = True,
    source_column_prefix: str = PASSTHROUGH_PREFIX,
) -> tuple[str, list[str]]:
    """Assemble the per-run projection SELECT and return ``(sql, orig_column_names)``.

    The canonical block is constant; the ``orig_<col>`` tail is generated from the keys
    actually present in this run's raw records (R21 — the reason this is a generated query
    and not a VIEW). ``orig_column_names`` is the ordered list of appended passthrough
    columns (empty when ``include_source_columns`` is False), so a caller knows the frame's
    trailing column set without re-parsing the SQL.
    """
    select_terms = [f"{expr} AS {name}" for name, expr in _CANONICAL_SELECT]
    orig_columns: list[str] = []
    if include_source_columns:
        fields = _orig_fields_in_first_appearance_order(connection)
        names = _passthrough_names(fields, source_column_prefix)
        for field in fields:
            alias = names[field]
            # The verbatim value of THIS assertion's own record. The JSON path quotes the
            # key (``$."<field>"``) so a field named e.g. ``a.party`` cannot break the
            # extract; single quotes are doubled for the SQL string literal, double quotes
            # for the JSON-path identifier.
            path = "'$.\"" + field.replace("'", "''").replace('"', '""') + "\"'"
            select_terms.append(
                f"json_extract(r.raw_json, {path}) AS {_quote_ident(alias)}")
            orig_columns.append(alias)
    sql = "SELECT\n  " + ",\n  ".join(select_terms) + "\n" + _FROM_JOINS
    return sql, orig_columns


def project(
    connection: sqlite3.Connection,
    *,
    include_source_columns: bool = True,
    source_column_prefix: str = PASSTHROUGH_PREFIX,
    ranking_method: str | None = None,
) -> pd.DataFrame:
    """Project the run database to the flat CSV frame — one row per assertion.

    HOW: resolve the current ranking method, build the generated SELECT, read it into a
    frame, restore the numeric-union TEXT columns to a numeric dtype (so they render
    unquoted), and fill the derived ``locates`` from ``EntityPositionLink`` — never from a
    SQL mapping (decision 11: the vocabulary is the single source of truth). Corner-less
    assertions keep their row with empty coordinate/time cells (LEFT JOINs), never dropped.
    """
    method = _resolve_ranking_method(connection, ranking_method)
    sql, _orig_columns = build_projection_sql(
        connection,
        include_source_columns=include_source_columns,
        source_column_prefix=source_column_prefix,
    )
    # HOW: build the frame column-wise from the raw cursor values rather than via
    # pd.read_sql_query. WHY: read_sql_query routes a nullable INTEGER column through
    # float64 before any dtype hint applies, silently rounding s2_cell_id (a signed int64
    # S2 cell beyond float64's 53-bit integer range — a §2-class corruption). Building
    # each _INTEGER_COLUMNS column as nullable Int64 straight from the sqlite ints keeps
    # every value exact; the remaining columns get the same per-column inference the old
    # dict-built frame had (float64 for pure floats, object/str for text).
    cursor = connection.execute(sql, [method])
    column_names = [description[0] for description in cursor.description]
    rows = cursor.fetchall()
    frame = pd.DataFrame({
        name: (pd.array([row[index] for row in rows], dtype="Int64")
               if name in _INTEGER_COLUMNS
               else [row[index] for row in rows])
        for index, name in enumerate(column_names)
    }, columns=column_names)
    for column in _NUMERIC_TEXT_COLUMNS:
        _restore_numeric_dtype(frame, column)
    _fill_locates(frame)
    return frame


def _restore_numeric_dtype(frame: pd.DataFrame, column: str) -> None:
    """Coerce a numeric-union TEXT column back to numeric where the values allow it.

    A column with at least one non-null value, all of which parse as numbers, becomes a
    numeric dtype (rendered unquoted); an all-null column, or one holding a non-numeric
    sentinel (``UTC_OFFSET_UNKNOWN`` = "N/A"), is left as object so it renders quoted —
    keeping "value, zone unknown" distinct from a real number.
    """
    series = frame[column]
    if not series.notna().any():
        return   # all-null -> object dtype -> quoted "" cells, matching the old frame
    try:
        frame[column] = pd.to_numeric(series)
    except (ValueError, TypeError):
        pass     # a non-numeric sentinel is present -> keep the text column verbatim


def _fill_locates(frame: pd.DataFrame) -> None:
    """Fill ``locates`` from ``entity_position_link`` via the vocabulary enum.

    WHY in Python and not SQL: ``Locates`` is a DERIVED view of the E-S edge (decision 11),
    declared once as ``EntityPositionLink.locates``; re-encoding the mapping as a SQL CASE
    would be a second source that could silently drift from the enum. An unmapped edge
    (NULL) stays empty; any stored value is a valid closed-vocabulary member (DB CHECK), so
    a lookup failure would be a real derivation bug and is allowed to surface.
    """
    frame["locates"] = [
        None if pd.isna(value) else V.EntityPositionLink(value).locates.value
        for value in frame["entity_position_link"]
    ]
