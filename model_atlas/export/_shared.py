"""Small value/formatting helpers shared by the export bricks.

Defines:    ``is_blank`` / ``clean`` / ``as_float`` (blank/NaN/NA-safe value
            coercion — the same defensive helpers ``exporters.builtin`` used for the
            old exporters), ``UNIX_EPOCH`` (the cross-platform epoch anchor for
            timedelta-based timestamp maths) and the spreadsheet formula-injection
            guard (``is_formula_trigger`` / ``neutralise_formula`` / ``guard_object_cells``
            — the ONE trigger table every text exporter shares).
Used by:    model_atlas.export.kml / .geojson / .xlsx / .geovisualizer / .csv_flat.
Depends on: pandas and the standard library only.

Why these helpers were re-declared rather than imported
---------------------------------------------------------
The old engine's exporters declared near-identical helpers for the OLD
``fn(ctx) -> list[Path]`` exporters. That module is fenced (read, never modify), and
importing from it would wire the new query+serialiser bricks back onto the
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


# --- spreadsheet formula-injection guard (OWASP CSV-injection) ----------------
# A cell whose FIRST character is one of these is interpreted as a live formula when the
# file is opened in Excel / Google Sheets / LibreOffice — so an attacker-controlled entity
# value like ``=HYPERLINK("http://evil","x")`` executes on open. The mitigation is the same
# for every text export, so the trigger table lives HERE once (one source of truth for the
# recognition rule) rather than being re-listed per writer. Tab and CR are included per
# OWASP guidance: some importers strip a leading whitespace char and re-expose the '=' after.
_FORMULA_TRIGGERS: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

# The neutralising prefix: a leading apostrophe forces the spreadsheet to treat the WHOLE
# cell as text (formula never evaluated). It is a single, known sentinel prepended verbatim,
# so a consumer recovers the original by stripping ONE leading apostrophe — the value is
# guarded, never mangled.
FORMULA_GUARD: str = "'"


def is_formula_trigger(value: Any) -> bool:
    """True when ``value`` is a string a spreadsheet would evaluate as a formula.

    WHY only str: numeric cells (float / Int64) carry no formula risk and must stay
    numeric — a negative NUMBER is a value, not a ``-``-led formula, so it is never guarded.
    """
    return isinstance(value, str) and value[:1] in _FORMULA_TRIGGERS


def neutralise_formula(value: Any) -> Any:
    """Prefix a formula-trigger string with the guard apostrophe; pass anything else through.

    Used by the CSV writers (csv_flat, geovisualizer), where a cell is plain text with no
    data-type marker — the leading apostrophe is the only way to force text. The xlsx writer
    instead forces the cell's data-type to text (keeping the value byte-exact); both share the
    ``is_formula_trigger`` rule so the two exports agree on WHAT is dangerous.
    """
    return FORMULA_GUARD + value if is_formula_trigger(value) else value


def guard_object_cells(frame: "pd.DataFrame") -> "pd.DataFrame":
    """A copy of ``frame`` with every TEXT-bearing cell run through ``neutralise_formula``.

    HOW: numeric / boolean / datetime columns are skipped (they carry no formula risk and MUST
    keep their dtype, so a QUOTE_NONNUMERIC writer still renders them unquoted and no
    coordinate/number is reshaped into a quoted string); every other column — object OR the
    pandas string dtype (this pandas infers ``StringDtype`` for text columns, which is NOT
    ``== object``) — is mapped. A None/NA cell passes through unchanged (still an empty cell).
    WHY a copy: the caller's frame is the shared flat view; mutating it would leak the guard
    into other exporters projecting the same store.
    """
    guarded = frame.copy()
    for column in guarded.columns:
        series = guarded[column]
        if (pd.api.types.is_numeric_dtype(series)
                or pd.api.types.is_bool_dtype(series)
                or pd.api.types.is_datetime64_any_dtype(series)):
            continue
        guarded[column] = series.map(neutralise_formula)
    return guarded
