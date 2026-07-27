"""GeoVisualizer export brick: the fixed 15-column CSV the GeoVisualizer tool imports
(T51 — ports ``exporters/builtin.py``'s ``export_geovisualizer``, including
its interval-expansion logic).

Defines:    GeovisualizerExporter (the ``Exporter`` protocol brick,
            ``format = "geovisualizer"``), registered in model_atlas.export.registry at
            import time.
Used by:    model_atlas.export.registry (registration) and
            tests/export/test_geovisualizer.py.
Depends on: model_atlas.core.flat_view (the flat query), model_atlas.export.csv_flat
            (the ``Exporter`` protocol / ``ExportContext``, ``CSV_DELIMITER`` via
            model_atlas.export), model_atlas.export._shared (value helpers +
            partitioning), pandas.

Query + serialiser, and the interval expansion (T51 scope)
--------------------------------------------------------------
The data access is ONE query — ``core.flat_view.build_flat_view`` — which already carries
BOTH temporal bounds per row (``time_lower_unix_utc_us`` / ``time_upper_unix_utc_us``,
sourced from ``timespan_integration.lower_unix_utc_us`` / ``upper_unix_utc_us``): the
"natural SELECT over timespan_integration" the relational model gives for free. The
expansion itself — one projected assertion becomes ONE GeoVisualizer row for an
instant (lower == upper, or only one bound present) or TWO for a genuine interval
(lower != upper, one row per bound) — is Python shaping, ported verbatim from the old
``_time_emissions``/``_geovis_description`` pair.

What changed from the old exporter (relational-model column split, finding F2)
------------------------------------------------------------------------------
The old flat model carried ONE ``utc_offset_hours`` column shared by both bounds; the
relational model splits it into ``utc_offset_hours_lower`` / ``_upper`` (F2), so each
emitted GeoVisualizer row now localises with, and warns on, ITS OWN bound's offset —
strictly more correct than the old single-offset check (a row for the upper bound
could previously report "offset known" while actually using the lower bound's value).

Encoding
--------
``utf-8-sig`` (with BOM): GeoVisualizer is a CSV consumed by an Excel-adjacent Java
importer on Windows, which the house rule's BOM auto-detection targets directly — same
choice the old exporter made.
"""
from __future__ import annotations

import csv
import re
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from model_atlas.core.flat_view import build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export import CSV_DELIMITER
from model_atlas.export._shared import UNIX_EPOCH, as_float, clean, is_blank
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter

# Fixed column layout expected by the GeoVisualizer tool (order matters).
_GEOVIS_COLUMNS: tuple[str, ...] = (
    "Target", "MSISDN", "IMEI", "Timestamp (local)", "Cell-ID", "Azimuth", "Address",
    "Latitude", "Longitude", "Description", "Source", "orig. Time", "Type", "Confidence", "Precision",
)


# --- IMEI extraction (ported verbatim) --------------------------------------

def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — the validity test an IMEI's 15th (check) digit enforces."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# The entity value may join several ids with '-', '_' or a space; split on any of them.
_IMEI_SEPARATORS = re.compile(r"[-_ ]")


def _pick_imei(entity: Any) -> str:
    """Split the entity on '-', '_' or space and return the FIRST token that is a valid
    15-digit IMEI (Luhn-checked), else '' — so a compound id like
    'dev_490154203237518-x' yields the IMEI."""
    text = clean(entity)
    if text is None:
        return ""
    for token in _IMEI_SEPARATORS.split(text):
        if len(token) == 15 and token.isdigit() and _luhn_ok(token):
            return token
    return ""


# --- timestamp formatting (ported verbatim) ---------------------------------

def _offset_suffix(delta: "timedelta | None") -> str:
    """A '+HH:MM' / '-HH:MM' UTC-offset suffix (strftime %z has no colon)."""
    total = int(delta.total_seconds()) if delta is not None else 0
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _local_timestamp(unix_us: Any, offset_hours: Any) -> str:
    """GeoVisualizer local timestamp: ``dd.mm.yyyy HH:MM:SS.ffffff+HH:MM`` (the UTC
    instant shifted by the row's own offset). '' when there is no timestamp. A
    non-numeric offset falls back to UTC — flagged by ``_geovis_description``."""
    if is_blank(unix_us):
        return ""
    try:
        microseconds = int(unix_us)
    except (TypeError, ValueError):
        return ""
    tz = timezone(timedelta(hours=as_float(offset_hours) or 0.0))
    local = (UNIX_EPOCH + timedelta(microseconds=microseconds)).astimezone(tz)
    return local.strftime("%d.%m.%Y %H:%M:%S.%f") + _offset_suffix(local.utcoffset())


# The description's first part when the emitted bound's offset is unknown: the shown
# time is the source wall-clock value and the '+00:00' suffix is a placeholder, not
# verified UTC.
_OFFSET_UNKNOWN_NOTE = "! UTC offset unknown - timestamp shown as-is (source time)"

# The target GeoVisualizer Java importer cannot render newlines inside a cell, so the
# multi-part description is joined with ' | ' (built in directly — never a post-hoc
# replace of '\n').
_DESCRIPTION_SEPARATOR = " | "


def _time_emissions(row: dict) -> "list[tuple[Any, Any, Any, str | None]]":
    """The (timestamp_us, source_field, utc_offset_hours, bound) tuples this projected
    row expands into.

    An instant (lower == upper, or only one bound present) yields ONE tuple with
    bound=None. A genuine interval (lower != upper) yields TWO — (lower, ..., 'start')
    and (upper, ..., 'end') — each carrying ITS OWN bound's source field and offset
    (F2 — the old model shared one offset for both). A row with no timestamp still
    yields one tuple (empty timestamp) so its position is not lost.
    """
    lower_us, upper_us = row.get("time_lower_unix_utc_us"), row.get("time_upper_unix_utc_us")
    lower_field, upper_field = row.get("time_lower_source_field"), row.get("time_upper_source_field")
    lower_offset, upper_offset = row.get("utc_offset_hours_lower"), row.get("utc_offset_hours_upper")
    lower_ok, upper_ok = not is_blank(lower_us), not is_blank(upper_us)
    if lower_ok and upper_ok:
        try:
            same = int(lower_us) == int(upper_us)
        except (TypeError, ValueError):
            same = str(lower_us) == str(upper_us)
        if not same:
            return [
                (lower_us, lower_field, lower_offset, "start"),
                (upper_us, upper_field, upper_offset, "end"),
            ]
        return [(lower_us, lower_field, lower_offset, None)]
    if lower_ok:
        return [(lower_us, lower_field, lower_offset, None)]
    if upper_ok:
        return [(upper_us, upper_field, upper_offset, None)]
    return [(None, lower_field or upper_field, lower_offset if lower_offset is not None else upper_offset, None)]


def _geovis_description(row: dict, timestamp_field: Any, offset_hours: Any, bound: "str | None") -> str:
    """Readable, mATLAS-tagged, single-line. Part order (joined with ' | '):
      1. (only when THIS bound's UTC offset is unknown) a clear warning that the time
         is shown as-is;
      2. Speed: <value> km/h (± accuracy) — or 'Speed: null';
      3. the source field of THE timestamp shown in this row;
      4. (only for an interval bound) 'Range start' / 'Range end' — the paired row
         holds the other;
      5. (mATLAS).
    """
    lines: list[str] = []
    if as_float(offset_hours) is None:
        lines.append(_OFFSET_UNKNOWN_NOTE)

    speed = as_float(row.get("horizontal_speed_kmh"))
    if speed is None:
        lines.append("Speed: null")
    else:
        speed_line = f"Speed: {speed:g} km/h"
        accuracy = as_float(row.get("horizontal_speed_accuracy_kmh"))
        if accuracy is not None:
            speed_line += f" (±{accuracy:g} km/h)"
        lines.append(speed_line)

    field = clean(timestamp_field)
    if field:
        lines.append(field)
    if bound == "start":
        lines.append("Range start")
    elif bound == "end":
        lines.append("Range end")

    lines.append("(mATLAS)")
    return _DESCRIPTION_SEPARATOR.join(lines)


def _geovis_source(row: dict) -> str:
    """The forensic origin: the in-artefact raw source path and the opened input file."""
    raw, opened = clean(row.get("raw_source_path")), clean(row.get("input_file_path"))
    if raw and opened:
        return f"{raw} ({opened})"
    return raw or opened or ""


def _num_or_blank(value: Any) -> float | str:
    number = as_float(value)
    return "" if number is None else number


class GeovisualizerExporter:
    """Fixed 15-column CSV for the GeoVisualizer tool (``format = "geovisualizer"``)."""

    format = "geovisualizer"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        frame = build_flat_view(
            store.connection, ranking_method=ctx.ranking_method, include_source_columns=False)
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        return [self._write(frame, ctx.output_dir / f"{ctx.base_name}.geovisualizer.csv")]

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        rows: list[dict] = []
        for row in frame.to_dict(orient="records"):
            # An interval assertion (lower != upper) expands into two rows, one per
            # time bound; an instant into one. All non-temporal columns (position,
            # IMEI, ...) are shared by both.
            for timestamp_us, timestamp_field, offset_hours, bound in _time_emissions(row):
                rows.append({
                    "Target": clean(row.get("linked_entity")) or "",
                    "MSISDN": "",
                    "IMEI": _pick_imei(row.get("entity")),
                    "Timestamp (local)": _local_timestamp(timestamp_us, offset_hours),
                    "Cell-ID": "",
                    "Azimuth": "",
                    "Address": "",
                    "Latitude": _num_or_blank(row.get("latitude_wgs84")),
                    "Longitude": _num_or_blank(row.get("longitude_wgs84")),
                    "Description": _geovis_description(row, timestamp_field, offset_hours, bound),
                    "Source": _geovis_source(row),
                    "orig. Time": "",
                    "Type": clean(row.get("preset_name")) or "",   # preset name (should always be present)
                    "Confidence": "",                    # reserved for the assertion link (later)
                    "Precision": _num_or_blank(row.get("horizontal_accuracy_m")),
                })
        out_frame = pd.DataFrame(rows, columns=list(_GEOVIS_COLUMNS))
        # ';' delimiter (Excel default) and MINIMAL quoting: a field is quoted only if
        # it contains the delimiter or a quote (the Description is single-line, joined
        # with ' | ', so needs no quoting).
        out_frame.to_csv(
            path, index=False, sep=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL, encoding="utf-8-sig")
        return path


register_exporter(
    "geovisualizer", "GeoVisualizer CSV", GeovisualizerExporter(),
    "Fixed 15-column CSV for the GeoVisualizer tool (Target/IMEI/local timestamp/lat-lon/...).",
)
