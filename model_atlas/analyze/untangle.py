"""Untangle (L3): the old rank.py grouping/ranking, re-homed onto the run database.

Defines:    ``analyse`` — reads every assertion joined to its entity and its L2
            integrations, applies the UNCHANGED untangle grouping/ranking semantics
            (``record_type`` main/additional + ``record_rank`` within same-second time
            groups, ranked by horizontal accuracy then completeness) and writes one
            method-stamped ``analysis`` row per assertion — plus ``METHOD_NAME`` /
            ``UNTANGLE_VERSION`` / ``DEFAULT_RESOLUTION_US``.
Used by:    the app-layer pipeline (T60) and tests/analyze/test_untangle.py.
Depends on: ``model_atlas.core`` (``ids`` — the analysis id derivation,
            ``vocabularies`` — the ``Analysis`` family + the ``RecordType`` labels,
            ``store`` — the layer-typed L3 writer, ``projection`` — the shared
            component subqueries, so this read and the flat CSV read the SAME
            component the same way) and pandas. The retired ``transforms/rank.py``
            was the normative reference: its ranking is ported here VERBATIM (see
            "The port", below), never modified.

The L3 contract (docs/run_db_schema.md decision 19)
---------------------------------------------------
Analysis is ABOUT assertions, never inside them: this brick runs after ingest and
integration and only ever writes the generic ``analysis`` table. Its ``method``
string is ``untangle/<version>``; re-running with a bumped version ADDS one row per
assertion (``UNIQUE(assertion_id, method)`` keeps the verdicts side by side and the
projection reads the current method's), while re-running the SAME version raises —
an in-place mutation of a recorded verdict would be silent loss, never allowed.
One row is written for EVERY assertion, ranked or not: a NULL verdict records
"examined, not part of a duplicate time group", which is evidence in itself and
distinct from "this method never ran".

The port (the retired transforms/rank.py ``untangle()``, semantics UNCHANGED)
-----------------------------------------------------------------
``_has_value`` / ``_completeness`` / ``_rank`` below are the old engine's code
verbatim (only the ``RecordType`` import moved to ``core.vocabularies`` — same
member values). The golden diff (T50) must be attributable ONLY to the schema move,
so nothing about the grouping key, the sort, or the tie-breaks may change here.
What DID move is the input: the old flat 45-column frame is reassembled from the
relational store by ``_ASSERTION_FRAME_SELECT``, each old flat column read from its
settled home (the csv_migration.md mapping):

- ``entity``                    -> ``entity.identifier`` (the flat contract's entity column);
- ``linked_entity``             -> ``entity.concerns``;
- ``time_*_raw`` / ``_source_field`` -> the ``lower``/``upper`` temporal components;
- ``time_*_unix_utc_us`` -> ``timespan_integration``; ``time_accuracy_us`` -> COALESCE of
  the two split per-bound ``*_accuracy_us`` columns (the completeness bit the old column had);
- ``utc_offset_hours``          -> COALESCE of the two split D32 applied offsets
  (the old single column existed once per row; either bound's applied offset marks
  the same "an offset is known" completeness bit the old column carried);
- ``temporal_source``           -> ``temporal_expression.temporal_source``;
- ``position_source``           -> ``spatial_expression.spatial_source`` (F3 rename);
- the coordinate/accuracy/speed/heading/beam metrics -> ``spatial_integration``;
- the three links               -> the ``assertion`` columns.

``ORDER BY a.rowid`` reproduces ingest order, so the old engine's insertion-order
tie-break (``_order``) is preserved bit-for-bit.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

import pandas as pd

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
# The component-picking subqueries are the projection's own (ordinal-ordered,
# LIMIT 1): importing them guarantees this brick ranks EXACTLY the value the flat
# CSV shows for the same column — a locally re-derived subquery could silently
# pick a different component row.
from model_atlas.core.flat_view import _spatial_part, _temporal_part
from model_atlas.core.store import RunStore

log = logging.getLogger(__name__)

# The method identity: ``untangle/<version>`` (decision 19). Bumping the version is
# how a changed ranker ADDS a new verdict generation; the docs' current generation
# is 2.0 (run_db_schema.md names ``untangle/2.0``).
METHOD_NAME = "untangle"
UNTANGLE_VERSION = "2.0"

# --- the old engine's constants, VERBATIM (transforms/rank.py) --------------

_LOWER_US = "time_lower_unix_utc_us"
_UPPER_US = "time_upper_unix_utc_us"
_ENTITY = "entity"
_LOWER_FIELD = "time_lower_source_field"
_UPPER_FIELD = "time_upper_source_field"
_HACC = "horizontal_accuracy_m"
_RECORD_TYPE = "record_type"
_RECORD_RANK = "record_rank"

# Acquisition tools round timestamps to the second; group at that resolution (1e6 us).
DEFAULT_RESOLUTION_US = 1_000_000

# Fields that count towards completeness when breaking an accuracy tie.
_COMPLETENESS_COLUMNS: tuple[str, ...] = (
    "entity", "linked_entity", "time_lower_raw", "time_lower_unix_utc_us",
    "time_upper_raw", "time_upper_unix_utc_us", "utc_offset_hours", "time_accuracy_us", "temporal_source",
    "latitude_wgs84", "longitude_wgs84", "altitude_m", "raw_position", "position_source",
    "horizontal_accuracy_m", "vertical_accuracy_m", "horizontal_speed_kmh",
    "vertical_speed_kmh", "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg",
    "beam_width_deg", "entity_position_link", "entity_time_link", "spatial_temporal_link",
)

# --- the store read: reassemble the old flat columns from their settled homes
# One (old flat column name, SQL expression) pair per column the ranking reads —
# the grouping key, the accuracy, and every completeness column. Aliases:
# a=assertion, e=entity, sx/si=spatial expression/integration, tx/ti=temporal
# expression/timespan integration (the projection's own alias vocabulary).
_ASSERTION_FRAME_SELECT: tuple[tuple[str, str], ...] = (
    ("assertion_id", "a.assertion_id"),
    ("entity", "e.identifier"),
    ("linked_entity", "e.concerns"),
    ("time_lower_raw", _temporal_part("lower", "raw_value")),
    ("time_lower_source_field", _temporal_part("lower", "source_field")),
    ("time_lower_unix_utc_us", "ti.lower_unix_utc_us"),
    ("time_upper_raw", _temporal_part("upper", "raw_value")),
    ("time_upper_source_field", _temporal_part("upper", "source_field")),
    ("time_upper_unix_utc_us", "ti.upper_unix_utc_us"),
    # The old flat model carried ONE applied offset per row; the settled schema
    # split it per bound (F2/D32). Either bound's applied offset carries the same
    # "an offset is known" completeness bit the old single column did.
    ("utc_offset_hours",
     "COALESCE(ti.utc_offset_hours_lower, ti.utc_offset_hours_upper)"),
    # The schema splits the timing uncertainty per bound; the ranking only needs the
    # "an accuracy is stated" completeness bit, so COALESCE the two into the one signal the
    # old flat model carried (the same shape as the utc_offset COALESCE just above).
    ("time_accuracy_us", "COALESCE(ti.lower_accuracy_us, ti.upper_accuracy_us)"),
    ("temporal_source", "tx.temporal_source"),
    ("latitude_wgs84", "si.latitude_wgs84"),
    ("longitude_wgs84", "si.longitude_wgs84"),
    ("altitude_m", "si.altitude_m"),
    ("raw_position", _spatial_part("raw_position", "raw_value")),
    ("position_source", "sx.spatial_source"),   # the F3 rename, read back under its old name
    ("horizontal_accuracy_m", "si.horizontal_accuracy_m"),
    ("vertical_accuracy_m", "si.vertical_accuracy_m"),
    ("horizontal_speed_kmh", "si.horizontal_speed_kmh"),
    ("vertical_speed_kmh", "si.vertical_speed_kmh"),
    ("heading_deg", "si.heading_deg"),
    ("heading_accuracy_deg", "si.heading_accuracy_deg"),
    ("beam_azimuth_deg", "si.beam_azimuth_deg"),
    ("beam_width_deg", "si.beam_width_deg"),
    ("entity_position_link", "a.entity_position_link"),
    ("entity_time_link", "a.entity_time_link"),
    ("spatial_temporal_link", "a.spatial_temporal_link"),
)

# LEFT JOINs: a corner-less assertion keeps its row (it simply ranks ineligible —
# the old engine's behaviour for a row with no decoded times). ORDER BY a.rowid is
# ingest order, the old frame's insertion order, preserving the ``_order`` tie-break.
_FROM_JOINS = """
FROM assertion a
JOIN entity e ON e.entity_id = a.entity_id
LEFT JOIN spatial_expression sx ON sx.spatial_expression_id = a.spatial_expression_id
LEFT JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id
LEFT JOIN temporal_expression tx ON tx.temporal_expression_id = a.temporal_expression_id
LEFT JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id
ORDER BY a.rowid
""".strip()


def _load_frame(connection: sqlite3.Connection) -> pd.DataFrame:
    """One row per assertion, columns = the old flat names the ranking reads."""
    select_terms = ",\n  ".join(f"{expr} AS {name}"
                                for name, expr in _ASSERTION_FRAME_SELECT)
    cursor = connection.execute(f"SELECT\n  {select_terms}\n{_FROM_JOINS}")
    column_names = [description[0] for description in cursor.description]
    return pd.DataFrame(cursor.fetchall(), columns=column_names)


# --- the ranking, ported VERBATIM from transforms/rank.py -------------------
# Any edit here would change the golden diff attribution — the T50 regeneration
# must trace every difference to the schema move, never to a ranking change.

def _has_value(value: Any) -> bool:
    if value is None or value is pd.NA:
        return False
    try:
        if bool(pd.isna(value)):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _completeness(out: pd.DataFrame) -> pd.Series:
    columns = [column for column in _COMPLETENESS_COLUMNS if column in out.columns]
    if not columns:
        return pd.Series([0] * len(out), index=out.index)
    return out[columns].apply(lambda row: sum(1 for value in row if _has_value(value)), axis=1)


def _rank(df: pd.DataFrame, *, resolution_us: int = DEFAULT_RESOLUTION_US) -> pd.DataFrame:
    """The old ``untangle()`` frame ranking, unchanged (transforms/rank.py).

    Time grouping resolution: acquisition tools round timestamps to the second, so
    two records of the same event differ only below the second. Grouping therefore
    compares timestamps at ``resolution_us`` granularity (default 1e6 us = whole
    seconds) — a unit/resolution choice, not a fuzzy tolerance.
    """
    out = df.copy()
    out[_RECORD_TYPE] = pd.NA
    out[_RECORD_RANK] = pd.NA
    required = {_LOWER_US, _UPPER_US, _ENTITY, _LOWER_FIELD, _UPPER_FIELD}
    if df.empty or not required.issubset(df.columns):
        return out

    out["_lower_num"] = pd.to_numeric(out[_LOWER_US], errors="coerce")
    out["_upper_num"] = pd.to_numeric(out[_UPPER_US], errors="coerce")
    eligible = out["_lower_num"].notna() & out["_upper_num"].notna()
    if not eligible.any():
        return out.drop(columns=["_lower_num", "_upper_num"])

    out["_hacc"] = pd.to_numeric(out[_HACC], errors="coerce") if _HACC in out.columns else pd.NA
    out["_hacc_missing"] = out["_hacc"].isna()
    out["_hacc_sort"] = out["_hacc"].fillna(float("inf"))
    out["_completeness"] = _completeness(out)
    out["_order"] = range(len(out))
    # Truncate to the tool's resolution so same-second records group together.
    out["_lower_unit"] = (out["_lower_num"] // resolution_us).astype("Int64")
    out["_upper_unit"] = (out["_upper_num"] // resolution_us).astype("Int64")

    group_cols = ["_lower_unit", "_upper_unit", _ENTITY, _LOWER_FIELD, _UPPER_FIELD]
    for _, group in out.loc[eligible].groupby(group_cols, dropna=False, sort=False):
        if len(group) < 2:
            continue
        ranked = group.sort_values(
            by=["_hacc_missing", "_hacc_sort", "_completeness", "_order"],
            ascending=[True, True, False, True],
            kind="mergesort",
        )
        ranks = pd.Series(range(1, len(ranked) + 1), index=ranked.index, dtype="Int64")
        out.loc[ranked.index, _RECORD_RANK] = ranks
        out.loc[ranked.index, _RECORD_TYPE] = ranks.apply(
            lambda value: V.RecordType.MAIN.value if value == 1
            else V.RecordType.ADDITIONAL.value
        )

    temp = ["_lower_num", "_upper_num", "_hacc", "_hacc_missing", "_hacc_sort",
            "_completeness", "_order", "_lower_unit", "_upper_unit"]
    return out.drop(columns=temp)


# --- the L3 entry point ------------------------------------------------------

def analyse(store: RunStore, *, version: str = UNTANGLE_VERSION,
            resolution_us: int = DEFAULT_RESOLUTION_US,
            clear_existing: bool = False) -> int:
    """Rank the store's assertions and write one ``analysis`` row per assertion.

    HOW: reassemble the old flat frame from the relational store (one row per
    assertion, ingest order), run the unchanged ranking, then write
    ``(assertion_id, untangle/<version>, record_type, record_rank)`` rows through
    the L3 writer. Returns the number of rows written (== the assertion count).
    Normally a re-run with a changed ranker ADDS a new method generation beside the
    old one (decision 19); re-running the SAME version hits ``UNIQUE(assertion_id,
    method)`` and raises, so a recorded verdict is never silently overwritten.

    ``clear_existing`` is the ONE sanctioned exception: on APPEND (a new run added to
    an existing case) the ranking must reflect the WHOLE case — a new row can join an
    existing time group — so this method's prior verdicts are deleted and RE-generated.
    That is a deliberate regeneration of DERIVED analysis (not evidence), distinct from
    the accidental overwrite the guard protects against.
    """
    method = f"{METHOD_NAME}/{version}"
    if clear_existing:
        store.connection.execute("DELETE FROM analysis WHERE method = ?", (method,))
    frame = _rank(_load_frame(store.connection), resolution_us=resolution_us)
    written = 0
    ranked = 0
    for assertion_id, record_type, record_rank in frame[
            ["assertion_id", _RECORD_TYPE, _RECORD_RANK]].itertuples(index=False):
        # pandas NA -> SQL NULL: an unranked assertion keeps its verdict row with a
        # NULL verdict ("examined, no duplicate group"), never a fabricated rank.
        rank_value = None if pd.isna(record_rank) else int(record_rank)
        store.write_analysis(V.Analysis(
            analysis_id=ids.analysis_id(assertion_id, method),
            assertion_id=assertion_id,
            method=method,
            record_type=None if pd.isna(record_type) else str(record_type),
            record_rank=rank_value,
        ))
        written += 1
        if rank_value is not None:
            ranked += 1
    log.info(f"Untangle {method}: wrote {written} analysis row(s), "
             f"{ranked} ranked, at {resolution_us} us resolution")
    return written
