"""Built-in additional exporters, shipped with mATLAS and registered on import.

Defines:    sqlite / geojson / xlsx / kml / geovisualizer exporters via @register_exporter.
Used by:    exporters.__init__ (imported for its registration side effects).
Depends on: exporters.registry, pandas, and the standard library (sqlite3, xml.etree, csv,
            json, math, datetime). Column names are the OUTPUT_COLUMNS literals produced by the
            assembly engine (model.families) — the same string-key idiom used across the codebase.

These double as worked examples of the drop-in exporter contract: fn(ctx) -> list[Path].
"""
from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ElementTree, SubElement

import pandas as pd

from model_atlas.export import CSV_DELIMITER
from model_atlas.exporters.registry import ExportContext, register_exporter

# Cross-platform epoch anchor (see transforms.builtin): timedelta arithmetic is valid across the
# whole datetime range, unlike datetime.fromtimestamp which raises on Windows for out-of-range us.
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --- small, defensive value helpers ----------------------------------------

def _is_blank(value: Any) -> bool:
    return (
        value is None
        or (isinstance(value, float) and math.isnan(value))
        or (isinstance(value, str) and value.strip() == "")
    )


def _clean(value: Any) -> str | None:
    """A trimmed string, or None when the cell is empty/NaN."""
    return None if _is_blank(value) else str(value).strip()


def _as_float(value: Any) -> float | None:
    """A float, or None when empty/NaN/non-numeric (e.g. utc_offset_hours == 'N/A')."""
    if _is_blank(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _out(ctx: ExportContext, tag: str, ext: str) -> Path:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    return ctx.output_dir / f"{ctx.base_name}.{tag}.{ext}"


# --- sqlite / geojson / xlsx / kml -----------------------------------------

@register_exporter("sqlite", "SQLite database", "All assertion rows in one SQLite table 'assertions'.")
def export_sqlite(ctx: ExportContext) -> list[Path]:
    out = _out(ctx, "assertions", "sqlite")
    if out.exists():
        out.unlink()   # start clean so no stale table/data survives across runs
    connection = sqlite3.connect(out)
    try:
        ctx.dataframe.to_sql("assertions", connection, if_exists="replace", index=False)
    finally:
        connection.close()
    return [out]


@register_exporter("geojson", "GeoJSON", "Point FeatureCollection of the rows that carry coordinates.")
def export_geojson(ctx: ExportContext) -> list[Path]:
    features: list[dict] = []
    for row in ctx.dataframe.to_dict(orient="records"):
        lat, lon = _as_float(row.get("latitude_wgs84")), _as_float(row.get("longitude_wgs84"))
        if lat is None or lon is None:
            continue   # a point feature needs a coordinate; rows without one are omitted here
        properties = {key: (None if _is_blank(val) else val)
                      for key, val in row.items() if key not in ("latitude_wgs84", "longitude_wgs84")}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": properties,
        })
    out = _out(ctx, "points", "geojson")
    out.write_text(
        json.dumps({"type": "FeatureCollection", "features": features},
                   indent=2, ensure_ascii=False, default=str),
        encoding="utf-8-sig",
    )
    return [out]


@register_exporter("xlsx", "Excel (.xlsx)", "Assertion rows as an Excel worksheet.")
def export_xlsx(ctx: ExportContext) -> list[Path]:
    out = _out(ctx, "assertions", "xlsx")
    ctx.dataframe.to_excel(out, index=False)   # openpyxl engine (already a dependency)
    return [out]


@register_exporter("kml", "KML", "Google-Earth placemarks with timestamps for located rows.")
def export_kml(ctx: ExportContext) -> list[Path]:
    kml = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    document = SubElement(kml, "Document")
    for row in ctx.dataframe.to_dict(orient="records"):
        lat, lon = _as_float(row.get("latitude_wgs84")), _as_float(row.get("longitude_wgs84"))
        if lat is None or lon is None:
            continue
        placemark = SubElement(document, "Placemark")
        SubElement(placemark, "name").text = _clean(row.get("linked_entity")) or ""
        when = _local_iso(row.get("time_lower_unix_utc_us"), row.get("utc_offset_hours"))
        if when:
            SubElement(SubElement(placemark, "TimeStamp"), "when").text = when
        SubElement(SubElement(placemark, "Point"), "coordinates").text = f"{lon},{lat}"
    out = _out(ctx, "points", "kml")
    ElementTree(kml).write(out, encoding="utf-8", xml_declaration=True)
    return [out]


# --- GeoVisualizer ----------------------------------------------------------

# Fixed column layout expected by the GeoVisualizer tool (order matters).
_GEOVIS_COLUMNS: tuple[str, ...] = (
    "Target", "MSISDN", "IMEI", "Timestamp (local)", "Cell-ID", "Azimuth", "Address",
    "Latitude", "Longitude", "Description", "Source", "orig. Time", "Type", "Confidence", "Precision",
)


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
    """Split the entity on '-', '_' or space and return the FIRST token that is a valid 15-digit
    IMEI (Luhn-checked), else '' — so a compound id like 'dev_490154203237518-x' yields the IMEI."""
    text = _clean(entity)
    if text is None:
        return ""
    for token in _IMEI_SEPARATORS.split(text):
        if len(token) == 15 and token.isdigit() and _luhn_ok(token):
            return token
    return ""


def _local_iso(unix_us: Any, offset_hours: Any) -> str:
    """ISO-8601 local timestamp: the absolute UTC instant shifted by the row's utc_offset_hours
    (so the wall-clock and the +HH:MM suffix match). '' when there is no timestamp."""
    if _is_blank(unix_us):
        return ""
    try:
        microseconds = int(unix_us)
    except (TypeError, ValueError):
        return ""
    tz = timezone(timedelta(hours=_as_float(offset_hours) or 0.0))
    return (_UNIX_EPOCH + timedelta(microseconds=microseconds)).astimezone(tz).isoformat()


def _offset_suffix(delta: "timedelta | None") -> str:
    """A '+HH:MM' / '-HH:MM' UTC-offset suffix (strftime %z has no colon)."""
    total = int(delta.total_seconds()) if delta is not None else 0
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _local_timestamp(unix_us: Any, offset_hours: Any) -> str:
    """GeoVisualizer local timestamp: ``dd.mm.yyyy HH:MM:SS.ffffff+HH:MM`` (the UTC instant shifted
    by the row's offset). '' when there is no timestamp. A non-numeric offset falls back to UTC —
    a known limitation the description flags (see _geovis_description)."""
    if _is_blank(unix_us):
        return ""
    try:
        microseconds = int(unix_us)
    except (TypeError, ValueError):
        return ""
    tz = timezone(timedelta(hours=_as_float(offset_hours) or 0.0))
    local = (_UNIX_EPOCH + timedelta(microseconds=microseconds)).astimezone(tz)
    return local.strftime("%d.%m.%Y %H:%M:%S.%f") + _offset_suffix(local.utcoffset())


# The description's first part when utc_offset_hours is unknown: we cannot localise, so the shown
# time is the source wall-clock value and the '+00:00' suffix is a placeholder, not verified UTC.
_OFFSET_UNKNOWN_NOTE = "! UTC offset unknown - timestamp shown as-is (source time)"

# The target GeoVisualizer Java importer cannot render newlines inside a cell, so the multi-part
# description is joined with ' | ' (built in directly — never a post-hoc replace of '\n').
_DESCRIPTION_SEPARATOR = " | "


def _time_emissions(row: dict) -> "list[tuple[Any, Any, str | None]]":
    """The (timestamp_us, source_field, bound) tuples this assertion row expands into.

    An instant (lower == upper, or only one present) yields ONE tuple with bound=None. A genuine
    interval (lower != upper) yields TWO — (lower, 'start') and (upper, 'end') — so each bound
    becomes its own GeoVisualizer row carrying the timestamp and the source field that produced it.
    A row with no timestamp still yields one tuple (empty timestamp) so its position is not lost."""
    lower_us, upper_us = row.get("time_lower_unix_utc_us"), row.get("time_upper_unix_utc_us")
    lower_field, upper_field = row.get("time_lower_source_field"), row.get("time_upper_source_field")
    lower_ok, upper_ok = not _is_blank(lower_us), not _is_blank(upper_us)
    if lower_ok and upper_ok:
        try:
            same = int(lower_us) == int(upper_us)
        except (TypeError, ValueError):
            same = str(lower_us) == str(upper_us)
        if not same:
            return [(lower_us, lower_field, "start"), (upper_us, upper_field, "end")]
        return [(lower_us, lower_field, None)]
    if lower_ok:
        return [(lower_us, lower_field, None)]
    if upper_ok:
        return [(upper_us, upper_field, None)]
    return [(None, lower_field or upper_field, None)]


def _geovis_description(row: dict, timestamp_field: Any, bound: "str | None") -> str:
    """Readable, mATLAS-tagged, single-line. Part order (joined with ' | '):
      1. (only when the UTC offset is unknown) a clear warning that the time is shown as-is;
      2. Speed: <value> km/h (± accuracy) — or 'Speed: null';
      3. the source field of THE timestamp shown in this row;
      4. (only for an interval bound) 'Range start' / 'Range end' — the paired row holds the other;
      5. (mATLAS)."""
    lines: list[str] = []
    # A non-numeric utc_offset_hours (e.g. 'N/A') means the local time could not be resolved and
    # _local_timestamp fell back to UTC — flag it first, before anything else, so it is unmissable.
    if _as_float(row.get("utc_offset_hours")) is None:
        lines.append(_OFFSET_UNKNOWN_NOTE)

    speed = _as_float(row.get("horizontal_speed_kmh"))
    if speed is None:
        lines.append("Speed: null")
    else:
        speed_line = f"Speed: {speed:g} km/h"
        accuracy = _as_float(row.get("horizontal_speed_accuracy_kmh"))
        if accuracy is not None:
            speed_line += f" (±{accuracy:g} km/h)"
        lines.append(speed_line)

    field = _clean(timestamp_field)
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
    raw, opened = _clean(row.get("raw_source_path")), _clean(row.get("input_file_path"))
    if raw and opened:
        return f"{raw} ({opened})"
    return raw or opened or ""


def _num_or_blank(value: Any) -> float | str:
    number = _as_float(value)
    return "" if number is None else number


@register_exporter(
    "geovisualizer", "GeoVisualizer CSV",
    "Fixed 15-column CSV for the GeoVisualizer tool (Target/IMEI/local timestamp/lat-lon/...).",
)
def export_geovisualizer(ctx: ExportContext) -> list[Path]:
    rows: list[dict] = []
    for row in ctx.dataframe.to_dict(orient="records"):
        # An interval assertion (lower != upper) expands into two rows, one per time bound; an
        # instant into one. All non-temporal columns (position, IMEI, ...) are shared by both.
        for timestamp_us, timestamp_field, bound in _time_emissions(row):
            rows.append({
                "Target": _clean(row.get("linked_entity")) or "",
                "MSISDN": "",
                "IMEI": _pick_imei(row.get("entity")),
                "Timestamp (local)": _local_timestamp(timestamp_us, row.get("utc_offset_hours")),
                "Cell-ID": "",
                "Azimuth": "",
                "Address": "",
                "Latitude": _num_or_blank(row.get("latitude_wgs84")),
                "Longitude": _num_or_blank(row.get("longitude_wgs84")),
                "Description": _geovis_description(row, timestamp_field, bound),
                "Source": _geovis_source(row),
                "orig. Time": "",
                "Type": _clean(row.get("preset_name")) or "",   # preset name (should always be present)
                "Confidence": "",                    # reserved for the assertion link (later)
                "Precision": _num_or_blank(row.get("horizontal_accuracy_m")),
            })
    frame = pd.DataFrame(rows, columns=list(_GEOVIS_COLUMNS))
    out = _out(ctx, "geovisualizer", "csv")
    # ';' delimiter (Excel default) and MINIMAL quoting: a field is quoted only if it contains the
    # delimiter or a quote (the Description is single-line, joined with ' | ', so needs no quoting).
    frame.to_csv(out, index=False, sep=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL, encoding="utf-8-sig")
    return [out]
