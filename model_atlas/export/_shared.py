"""Small value/formatting helpers shared by the T51 export bricks.

Defines:    ``is_blank`` / ``clean`` / ``as_float`` (blank/NaN/NA-safe value
            coercion — the same defensive helpers ``exporters.builtin`` used for the
            old exporters) and ``UNIX_EPOCH`` (the cross-platform epoch anchor for
            timedelta-based timestamp maths).
Used by:    model_atlas.export.kml / .geojson / .xlsx / .geovisualizer.
Depends on: pandas and the standard library only.

Why these helpers were re-declared rather than imported (T51)
-------------------------------------------------------------------
The old engine's exporters declared near-identical helpers for the OLD
``fn(ctx) -> list[Path]`` exporters. That module is fenced (read, never modify) for
T51, and importing from it would wire the new query+serialiser bricks back onto the
old engine's ``ExportContext`` shape, so these are re-declared here as this
package's own small, dependency-free copy.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

# Cross-platform epoch anchor (mirrors integrate.timeparse / the old
# exporters.builtin): timedelta arithmetic is valid across the whole datetime range,
# unlike datetime.fromtimestamp, which raises on Windows for out-of-range microseconds.
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_blank(value: Any) -> bool:
    """True for None, an empty/whitespace string, float NaN, or pandas NA/NaT.

    WHY pandas NA too: ``core.flat_view.build_flat_view`` reads several columns (the epoch
    micros, ``s2_cell_id``…) as nullable ``Int64``, whose missing marker is
    ``pandas.NA`` — plain ``math.isnan`` (the old exporter's check) does not
    recognise it and would treat a missing value as present.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def clean(value: Any) -> str | None:
    """A trimmed string, or None when the cell is empty/NaN/NA."""
    return None if is_blank(value) else str(value).strip()


def as_float(value: Any) -> float | None:
    """A float, or None when empty/NaN/NA/non-numeric (e.g. a text offset sentinel)."""
    if is_blank(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
