"""Untangle: rank co-located assertions within a time group (new model).

Defines:    untangle() — assigns record_type (main/additional) and record_rank to
            assertions that fall in the same time group, ranked by horizontal accuracy
            then completeness.
Used by:    the v2 pipeline.
Depends on: model.families column names, pandas.

Time grouping resolution: acquisition tools round timestamps to the second, so two
records of the same event differ only below the second. Grouping therefore compares
timestamps at ``resolution_ns`` granularity (default 1e9 ns = whole seconds) — a
unit/resolution choice, not a fuzzy tolerance.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from model_atlas.model.families import RecordType

log = logging.getLogger(__name__)

_LOWER_NS = "time_lower_unix_ns"
_UPPER_NS = "time_upper_unix_ns"
_ENTITY = "entity"
_LOWER_FIELD = "time_lower_source_field"
_UPPER_FIELD = "time_upper_source_field"
_HACC = "horizontal_accuracy_m"
_RECORD_TYPE = "record_type"
_RECORD_RANK = "record_rank"

# Acquisition tools round timestamps to the second; group at that resolution (1e9 ns).
DEFAULT_RESOLUTION_NS = 1_000_000_000

# Fields that count towards completeness when breaking an accuracy tie.
_COMPLETENESS_COLUMNS: tuple[str, ...] = (
    "entity", "linked_entity", "time_lower_raw", "time_lower_unix_ns",
    "time_upper_raw", "time_upper_unix_ns", "time_zone", "time_accuracy_ns", "temporal_source",
    "latitude_wgs84", "longitude_wgs84", "altitude_m", "raw_position", "position_source",
    "horizontal_accuracy_m", "vertical_accuracy_m", "horizontal_speed_kmh",
    "vertical_speed_kmh", "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg",
    "beam_width_deg", "entity_position_link", "entity_time_link", "spatial_temporal_link",
)


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


def untangle(df: pd.DataFrame, *, resolution_ns: int = DEFAULT_RESOLUTION_NS) -> pd.DataFrame:
    out = df.copy()
    out[_RECORD_TYPE] = pd.NA
    out[_RECORD_RANK] = pd.NA
    required = {_LOWER_NS, _UPPER_NS, _ENTITY, _LOWER_FIELD, _UPPER_FIELD}
    if df.empty or not required.issubset(df.columns):
        return out

    out["_lower_num"] = pd.to_numeric(out[_LOWER_NS], errors="coerce")
    out["_upper_num"] = pd.to_numeric(out[_UPPER_NS], errors="coerce")
    eligible = out["_lower_num"].notna() & out["_upper_num"].notna()
    if not eligible.any():
        return out.drop(columns=["_lower_num", "_upper_num"])

    out["_hacc"] = pd.to_numeric(out[_HACC], errors="coerce") if _HACC in out.columns else pd.NA
    out["_hacc_missing"] = out["_hacc"].isna()
    out["_hacc_sort"] = out["_hacc"].fillna(float("inf"))
    out["_completeness"] = _completeness(out)
    out["_order"] = range(len(out))
    # Truncate to the tool's resolution so same-second records group together.
    out["_lower_unit"] = (out["_lower_num"] // resolution_ns).astype("Int64")
    out["_upper_unit"] = (out["_upper_num"] // resolution_ns).astype("Int64")

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
            lambda value: RecordType.MAIN.value if value == 1 else RecordType.ADDITIONAL.value
        )

    temp = ["_lower_num", "_upper_num", "_hacc", "_hacc_missing", "_hacc_sort",
            "_completeness", "_order", "_lower_unit", "_upper_unit"]
    result = out.drop(columns=temp)
    ranked_rows = int(result[_RECORD_RANK].notna().sum())
    log.info("Untangle complete: %d ranked row(s) at %d ns resolution", ranked_rows, resolution_ns)
    return result
