"""The flat CSV view: a GENERATED query over the run database.

Defines:    ``CANONICAL_COLUMNS`` (the ordered flat-CSV column contract encoded from
            [docs/csv_migration.md](../../docs/csv_migration.md)), ``build_flat_view_sql``
            (assembles the per-run SELECT — canonical block first, then the exploded
            ``orig_<col>`` passthrough), and ``build_flat_view`` (runs it against a
            read-only connection and returns the flat pandas frame with numeric dtypes
            restored).
Used by:    the CSV exporter and any read-only consumer wanting the flat view;
            tests.core.test_flat_view. NB: this is the CSV-column "flat view", distinct
            from ids.py's ``l1_projection`` (the digest input) and the future road
            geometry "projection" — the three no longer share the one word.
Depends on: ``model_atlas.core.vocabularies`` (``RecordType`` for the derived
            the enrichment marker driving the ``_matlas`` suffix),
            ``pandas`` (the frame + numeric
            re-coercion the old frame builder did), and the standard library
            (``sqlite3``, ``json``).

Why a GENERATED query and not a static VIEW
--------------------------------------------
The canonical columns are fixed, but the ``orig_<col>`` verbatim passthrough is the
UNION of every raw record's own keys — a set that differs per source and therefore per
run. A static VIEW cannot carry a per-run column set, so the flat view is assembled
as a SQL string once per run: the canonical SELECT list is constant, the ``orig_``
tail is computed from the records actually present.

One row PER OBSERVATION
----------------------
An observation exists only per placement, so the line count IS the placement count (a
one-legged CDR is one line, a 20-recipient message is one). The owned corners
(entity is mandatory; the spatial/temporal expressions are optional) are LEFT JOINed,
so a corner-less observation yields EMPTY coordinate/time cells — never dropped, never
invented (the §2 "positionless claim vanishes" guard).

Column-contract decisions (recorded where csv_migration.md is a mapping, not a literal
column list)
------------------------------------------------------------------------------------
- Order = the migration's "Every old column, mapped" block first (for reviewer
  diffing), then the "New columns" block, then ``orig_<col>`` (its regeneration
  procedure).
- ``position_source`` -> ``spatial_source`` (``position_type``/``spatial_role`` are gone:
  the role was a preset-author assertion, and ``*_source_field`` carries the meaning)
  (cross-check row 42: the settled model's names; the old headers are
  retired).
- ``utc_offset_hours`` -> ``offset_to_local_hours_lower`` / ``_upper`` — hours to ADD to
  the canonical UTC µs to read the bound in the case's configured local zone, NOT the
  offset the source's clock carried. The rendered ``*_datetime_iso`` / ``*_date`` /
  ``*_time`` columns are gone with it: ``core.local_time`` renders on demand.
- ``deleted`` is GONE (decision 48): the app's own flag asked a question the source could
  not answer separately from "was this recovered", so it folded into ``recovery_state`` —
  which now sources from ``observation``, not ``raw_record``.
- ``record_order`` reads ``analysis.record_rank_type`` directly. The label is STORED
  (decision 54) so SQL against this database can use it; the projection no longer derives it.
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
# Table aliases: a=observation, r=raw_record, s=source, rn=run, c=case_metadata,
# e=entity, p=preset, sx/si/six=spatial expression/integration/index,
# tx/ti=temporal expression/timespan integration, an=analysis.

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


def _temporal_part_either(first: str, second: str, column: str) -> str:
    """``_temporal_part`` over two alternative part names, preferring ``first``.

    For a slot a source may spell two ways (an upper bound as an end clock OR as a duration):
    the parts are mutually exclusive per expression, so COALESCE picks whichever was written
    without either spelling shadowing the other.
    """
    return (f"COALESCE({_temporal_part(first, column)}, "
            f"{_temporal_part(second, column)})")


def _spatial_part(part: str, column: str) -> str:
    """Correlated subquery picking one spatial component's ``column`` for ``part``."""
    return (f"(SELECT sc.{column} FROM spatial_component sc "
            f"WHERE sc.spatial_expression_id = a.spatial_expression_id "
            f"AND sc.part = '{part}' ORDER BY sc.ordinal LIMIT 1)")


# raw_source_path: the path the source REPORTS the data originally lived at (secondary
# exports only — NULL for a primary acquisition), falling back to the chain-inclusive
# input source path (``input_file_path!inner_path``), which for a primary IS the truth.
_RAW_SOURCE_PATH = (
    "COALESCE(s.reported_data_source, CASE WHEN s.inner_path IS NOT NULL "
    "THEN s.input_file_path || '!' || s.inner_path ELSE s.input_file_path END)")

_CANONICAL_SELECT: tuple[tuple[str, str], ...] = (
    # -- Block 1: every old column, mapped (csv_migration.md, old CSV order) ----
    ("entity", "e.identifier"),
    ("entity_type", "e.type"),
    ("time_lower_raw", _temporal_part("lower", "raw_value")),
    ("time_lower_source_field", _temporal_part("lower", "source_field")),
    ("time_lower_unix_utc_us", "ti.lower_unix_utc_us"),
    # The upper bound is written EITHER as an end clock (part ``upper``) or as an elapsed span
    # (part ``duration`` — the CDR start+duration shape); exactly one exists per expression and
    # both fill the same upper slot, so the projection coalesces them. Without this the
    # source's own verbatim span never reaches the export and only the DERIVED end is visible —
    # the L1 evidence silently missing from the row that rests on it.
    ("time_upper_raw", _temporal_part_either("upper", "duration", "raw_value")),
    ("time_upper_source_field", _temporal_part_either("upper", "duration", "source_field")),
    ("time_upper_unix_utc_us", "ti.upper_unix_utc_us"),
    ("offset_to_local_hours_lower", "ti.offset_to_local_hours_lower"),
    ("offset_to_local_hours_upper", "ti.offset_to_local_hours_upper"),
    ("lower_accuracy_us", "ti.lower_accuracy_us"),
    ("upper_accuracy_us", "ti.upper_accuracy_us"),
    ("temporal_source", "tx.temporal_source"),
    # The value columns read the canonical L2 integration columns (which KEEP their
    # ``*_wgs84`` names); the ``*_source_field`` subqueries key off the datum-agnostic L1
    # component part names ``latitude`` / ``longitude`` — so the CSV header is unchanged
    # while the lookup follows the renamed L1 part.
    ("latitude_wgs84", "si.latitude_wgs84"),
    ("latitude_source_field", _spatial_part("latitude", "source_field")),
    ("longitude_wgs84", "si.longitude_wgs84"),
    ("longitude_source_field", _spatial_part("longitude", "source_field")),
    ("altitude_m", "si.altitude_m"),
    # The verbatim components that produced latitude_wgs84 / longitude_wgs84, as
    # `<source column>:<value> | ...` — written by the spatial integration, which is the
    # only layer that knows which components ITS derivation consumed. It replaces the old
    # `raw_position`, which projected the single composite component and was therefore
    # EMPTY for every preset that maps structured lat/lon columns (i.e. most of them).
    ("position_raw", "si.position_raw"),
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
    ("entity_link", "a.entity_link"),
    ("spatial_link", "a.spatial_link"),
    ("temporal_link", "a.temporal_link"),
    ("raw_source_path", _RAW_SOURCE_PATH),
    ("input_file_path", "s.input_file_path"),
    ("input_record_id", "a.input_record_id"),
    # The preset-mapped source uid WINS over the engine-generated one: when the source has a
    # genuine key (a tool's Item ID) the examiner must see THAT, not a derived uuid. NULL when
    # unmapped (or `source_record_uid: default`), so the generated id remains the fallback.
    ("source_record_uid", "COALESCE(a.source_record_uid, r.raw_record_id)"),
    ("row_uid", "a.observation_id"),
    ("preset_id", "p.key"),                             # the preset KEY (migration row 61)
    ("preset_name", "p.name"),
    ("source_label", "a.source_label"),
    ("source_tier", "s.source_tier"),
    ("record_rank", "an.record_rank"),
    ("record_order", "an.record_rank_type"),            # STORED (decision 54)
    # -- Block 2: new columns (csv_migration.md order) -------------------------
    ("case_number", "c.case_number"),
    ("observation_content_uid", "a.observation_content_hash"),
    ("spatiotemporal_entity_hash", "a.spatiotemporal_entity_hash"),
    ("raw_identifier", "e.raw_identifier"),
    ("identifier_type", "e.identifier_type"),
    # The position BEARER, projected from the SECOND entity join. This is the accepted
    # storage/projection inversion made concrete: storage orders by whose position it is, so
    # a CDR row stores the antenna, while the line still names the phone and the antenna
    # arrives as columns instead of dissolving into a cell_ref string. For `subject` the
    # bearer IS the targeted entity, so these repeat it; empty for `unknown` (decision 55).
    ("bearer_entity", "be.identifier"),
    ("bearer_raw_identifier", "be.raw_identifier"),
    ("bearer_identifier_type", "be.identifier_type"),
    ("bearer_entity_type", "be.type"),
    # Both read the OBSERVATION, never raw_record: on a primary source assemble copied our
    # own L0 finding onto it, on a secondary one it holds what the TOOL reports. One column
    # in the output either way, and the retired `deleted` column's question folded into it.
    ("recovery_state", "a.recovery_state"),
    # The fine origin (wal/journal/freelist/freespace); NULL means the record was live,
    # i.e. recovered from nowhere — a populated cell IS the "this was recovered" signal.
    ("recovery_source", "a.recovery_source"),
    ("reported_data_source", "s.reported_data_source"),
    ("s2_cell_id", "six.s2_cell_id"),
)

# The ordered flat-CSV column names — the encoded csv_migration.md contract. The exporter
# and the tests assert the projected frame's canonical block matches this exactly.
# --- the ``_matlas`` enrichment suffix --------------------------------------
# A projected column is suffixed when its value did not come from the trace: a uid mATLAS
# computed, a link a preset author stated, a rank an analysis produced, the operator's case
# number. A value DERIVED from a trace datum in an obvious way keeps its bare name —
# ``latitude_wgs84`` is still the source's own coordinate, just converted.
#
# The flag is DERIVED, not listed: every simple ``alias.column`` expression is resolved back
# to its model field and read from the marker declared in vocabularies. The alias map below
# is the same one ``_FROM_JOINS`` declares, so a renamed table cannot silently un-suffix a
# column. Only expressions with no single model field (a COALESCE, a correlated subquery, a
# Python-filled ``NULL``) are named explicitly.

_ALIAS_MODELS: dict[str, type] = {
    "a": V.Observation, "e": V.Entity, "be": V.Entity, "s": V.SourceFile, "r": V.RawRecord,
    "p": V.Preset, "c": V.CaseMetadata, "act": V.Activity, "an": V.Analysis,
    "sx": V.SpatialExpression, "si": V.SpatialIntegration, "six": V.SpatialIndex,
    "tx": V.TemporalExpression, "ti": V.TimespanIntegration,
}

# Composite expressions: no single model field to read the marker from.
_COMPUTED_ENRICHMENT: frozenset[str] = frozenset({
    # COALESCE(a.source_record_uid, r.raw_record_id) — the fallback half is a mATLAS uid, and
    # a uid is an enrichment even though it is derived, because nothing in the trace is it.
    "source_record_uid",
    "record_order",          # analysis.record_rank_type
})
_COMPUTED_TRACE: frozenset[str] = frozenset({
    "raw_source_path",       # COALESCE over source paths — the acquisition's own
})


def _is_enrichment_column(name: str, expr: str) -> bool:
    """Whether a projected column carries a mATLAS enrichment (and so takes the suffix)."""
    if name in _COMPUTED_ENRICHMENT:
        return True
    if name in _COMPUTED_TRACE:
        return False
    alias, _, column = expr.partition(".")
    model = _ALIAS_MODELS.get(alias)
    # Anything else is a subquery or literal over verbatim source values (the ``*_raw`` and
    # ``*_source_field`` parts), which are the trace's own words by construction.
    if model is None or not column.isidentifier():
        return False
    return V.is_enrichment(model, column)


ENRICHMENT_SUFFIX = "_matlas"

CANONICAL_COLUMNS: tuple[str, ...] = tuple(
    name + ENRICHMENT_SUFFIX if _is_enrichment_column(name, expr) else name
    for name, expr in _CANONICAL_SELECT)

# Columns whose MODEL type is a numeric union (``float | str`` / ``int | float``) so the
# schema stores them as TEXT, but whose values are semantically numeric. After reading the
# frame back their numeric dtype is restored PER VALUE (see _restore_numeric_dtype) so the
# CSV renders each real number unquoted, while a non-numeric sentinel (``LOCAL_OFFSET_UNKNOWN``
# = "N/A") sitting in a sibling row stays text (quoted) — keeping "no local zone configured"
# visibly distinct from a real offset without dragging the genuine numbers into TEXT too.
_NUMERIC_TEXT_COLUMNS: tuple[str, ...] = (
    "offset_to_local_hours_lower", "offset_to_local_hours_upper",
)

# INTEGER-typed flat-view columns that may carry NULLs (a corner-less observation). Read
# as pandas nullable ``Int64`` rather than the float64 pandas would otherwise infer:
# ``s2_cell_id`` is a signed int64 S2 leaf cell whose value EXCEEDS float64's 53-bit
# integer range — a float round-trip would silently corrupt the cell id (a §2-class
# silent loss) — and the epoch columns render as clean unquoted integers.
# NOTE: these name PROJECTED columns, so enrichment members carry the ``_matlas`` suffix —
# the frame is keyed on the final name, not the model field.
_INTEGER_COLUMNS: tuple[str, ...] = (
    "time_lower_unix_utc_us", "time_upper_unix_utc_us",
    "lower_accuracy_us", "upper_accuracy_us",
    "record_rank_matlas", "s2_cell_id_matlas",
)

# The join chain up the grain (observation -> raw_record -> source -> run -> case) plus the
# owned corners, their integrations, the S2 index and the current analysis method. The
# ``?`` binds the resolved ranking method (see build_flat_view_sql). ORDER BY a.rowid
# reproduces ingest order, so the flat frame is deterministic.
_FROM_JOINS = """
FROM observation a
JOIN raw_record r ON r.raw_record_id = a.raw_record_id
JOIN source_file s ON s.source_file_id = r.source_file_id
JOIN run rn ON rn.run_id = s.run_id
JOIN case_metadata c ON c.case_id = rn.case_id
JOIN entity e ON e.entity_id = a.entity_id
LEFT JOIN entity be ON be.entity_id = a.bearer_entity_id
JOIN preset p ON p.preset_id = a.preset_id
LEFT JOIN spatial_expression sx ON sx.spatial_expression_id = a.spatial_expression_id
LEFT JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id
LEFT JOIN spatial_index six ON six.spatial_integration_id = si.spatial_integration_id
LEFT JOIN temporal_expression tx ON tx.temporal_expression_id = a.temporal_expression_id
LEFT JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id
LEFT JOIN analysis an ON an.observation_id = a.observation_id AND an.method = ?
ORDER BY a.rowid
""".strip()
# NOTE (future-proofing): when plural integration candidates arrive, add
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


def _json_path_literal(field: str) -> str:
    """Build the SQL string literal of the JSON path ``$."<field>"`` for one raw key.

    Two DISTINCT escaping layers stack here and must not be conflated (conflating them was
    the original bug — SQL-literal ``"`` -> ``""`` doubling was mistakenly applied to the
    JSON label, and backslashes were not escaped at all):

    HOW:
    - JSON-path layer (inside the ``$."..."`` quoted label): SQLite's JSON path parser
      treats backslash as the ESCAPE character, so a literal ``\\`` must become ``\\\\`` and
      a literal ``"`` must become ``\\"`` (backslash-escaped — NOT SQL-style ``""``). The
      backslash escape is applied FIRST so the backslash introduced for a following ``"`` is
      not doubled a second time.
    - SQL string-literal layer (the whole path is embedded as a ``'...'`` literal): an
      embedded single quote is doubled ``''``. Backslash is not special in a SQLite string
      literal, so it needs no SQL-layer handling — only the JSON layer's.

    WHY it matters: ``json_extract`` with a mis-escaped path silently returns NULL for every
    row (no error raised), so a raw field named e.g. ``a"b`` or one containing ``\\`` would
    emit its ``orig_`` passthrough column as all-NULL — silent verbatim-data loss.
    """
    json_label = field.replace("\\", "\\\\").replace('"', '\\"')
    sql_escaped = json_label.replace("'", "''")
    return "'$.\"" + sql_escaped + "\"'"


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

    WHY MAX when unspecified: re-running a changed ranker ADDS rows rather than overwriting,
    method strings are versioned (``untangle/2.0``), so the lexicographic maximum is the latest —
    the "current" verdict. The caller may pin a specific method explicitly.
    """
    if ranking_method is not None:
        return ranking_method
    row = connection.execute("SELECT MAX(method) FROM analysis").fetchone()
    return row[0] if row is not None else None


def build_flat_view_sql(
    connection: sqlite3.Connection,
    *,
    include_source_columns: bool = True,
    source_column_prefix: str = PASSTHROUGH_PREFIX,
) -> tuple[str, list[str]]:
    """Assemble the per-run flat-view SELECT and return ``(sql, orig_column_names)``.

    The canonical block is constant; the ``orig_<col>`` tail is generated from the keys
    actually present in this run's raw records (the reason this is a generated query
    and not a VIEW). ``orig_column_names`` is the ordered list of appended passthrough
    columns (empty when ``include_source_columns`` is False), so a caller knows the frame's
    trailing column set without re-parsing the SQL.
    """
    select_terms = [f"{expr} AS {final}"
                    for (name, expr), final in zip(_CANONICAL_SELECT,
                                                   CANONICAL_COLUMNS)]
    orig_columns: list[str] = []
    if include_source_columns:
        fields = _orig_fields_in_first_appearance_order(connection)
        names = _passthrough_names(fields, source_column_prefix)
        for field in fields:
            alias = names[field]
            # The verbatim value of THIS observation's own record. The JSON path quotes the
            # key (``$."<field>"``) so a field named e.g. ``a.party`` cannot break the
            # extract; _json_path_literal handles both escaping layers (JSON-path backslash
            # escaping + SQL-literal single-quote doubling) so an exotic key never yields a
            # silently all-NULL passthrough column.
            path = _json_path_literal(field)
            select_terms.append(
                f"json_extract(r.raw_json, {path}) AS {_quote_ident(alias)}")
            orig_columns.append(alias)
    sql = "SELECT\n  " + ",\n  ".join(select_terms) + "\n" + _FROM_JOINS
    return sql, orig_columns


def build_flat_view(
    connection: sqlite3.Connection,
    *,
    include_source_columns: bool = True,
    source_column_prefix: str = PASSTHROUGH_PREFIX,
    ranking_method: str | None = None,
) -> pd.DataFrame:
    """Build the flat CSV frame from the run database — one row per observation.

    HOW: resolve the current ranking method, build the generated SELECT, read it into a
    frame, restore the numeric-union TEXT columns to a numeric dtype (so they render
    unquoted) — never from a
    SQL mapping (the vocabulary is the single source of truth). Corner-less
    observations keep their row with empty coordinate/time cells (LEFT JOINs), never dropped.
    """
    method = _resolve_ranking_method(connection, ranking_method)
    sql, _orig_columns = build_flat_view_sql(
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
    return frame


def _restore_numeric_dtype(frame: pd.DataFrame, column: str) -> None:
    """Coerce a numeric-union TEXT column back to numeric PER VALUE.

    A column whose non-null cells ALL parse as numbers becomes a numeric dtype (rendered
    unquoted). When only SOME cells are numbers — the rest being the non-numeric sentinel
    (``LOCAL_OFFSET_UNKNOWN`` = "N/A") — the genuine numbers are still restored to numeric
    values while the sentinel cells keep their text, so a mixed column renders each number
    numerically and each sentinel as the sentinel (rather than the WHOLE column staying
    TEXT and rendering every real offset quoted, e.g. ``'2.00'``).

    HOW: ``pd.to_numeric(..., errors="coerce")`` yields a numeric value where the cell
    parses and NaN where it does not; ``errors="coerce"`` (not a try/except) keeps this a
    value-level decision rather than exceptions-as-control-flow. An all-null column, and any
    genuinely non-numeric cell, is left as its original object value so it renders quoted.
    """
    series = frame[column]
    if not series.notna().any():
        return   # all-null -> object dtype -> quoted "" cells, matching the old frame
    coerced = pd.to_numeric(series, errors="coerce")
    present = series.notna()
    if coerced[present].notna().all():
        # No sentinel present: every non-null cell is a number -> a single numeric dtype
        # (identical to the old whole-column path, so a sentinel-free golden never drifts).
        frame[column] = coerced
        return
    # Mixed: rebuild an object column cell-by-cell — the parsed number where the cell is
    # numeric, else the original value (the text sentinel), else None for a genuinely empty
    # cell. QUOTE_NONNUMERIC then renders the numeric cells unquoted and the sentinel quoted.
    frame[column] = [
        number if pd.notna(number) else (None if pd.isna(original) else original)
        for original, number in zip(series, coerced)
    ]
