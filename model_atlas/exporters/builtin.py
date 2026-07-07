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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ElementTree, SubElement

import pandas as pd

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


def _pick_imei(entity: Any) -> str:
    """The entity value is split on '_'; return the first token that is a valid 15-digit IMEI
    (Luhn-checked), else '' — so a compound id like 'dev_490154203237518_x' yields the IMEI."""
    text = _clean(entity)
    if text is None:
        return ""
    for token in text.split("_"):
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


def _geovis_description(row: dict) -> str:
    """Short, readable, mATLAS-tagged: the timestamp source field (one name when the lower and
    upper source fields are identical), plus speed / speed-accuracy in km/h when present."""
    lower, upper = _clean(row.get("time_lower_source_field")), _clean(row.get("time_upper_source_field"))
    if lower and upper and lower != upper:
        timestamp_field = f"{lower} / {upper}"
    else:
        timestamp_field = lower or upper or ""
    parts = ["mATLAS"]
    if timestamp_field:
        parts.append(timestamp_field)
    speed = _as_float(row.get("horizontal_speed_kmh"))
    if speed is not None:
        text = f"{speed:g} km/h"
        accuracy = _as_float(row.get("horizontal_speed_accuracy_kmh"))
        if accuracy is not None:
            text += f" (±{accuracy:g} km/h)"
        parts.append(text)
    return " · ".join(parts)


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
        rows.append({
            "Target": _clean(row.get("linked_entity")) or "",
            "MSISDN": "",
            "IMEI": _pick_imei(row.get("entity")),
            "Timestamp (local)": _local_iso(row.get("time_lower_unix_utc_us"), row.get("utc_offset_hours")),
            "Cell-ID": "",
            "Azimuth": "",
            "Address": "",
            "Latitude": _num_or_blank(row.get("latitude_wgs84")),
            "Longitude": _num_or_blank(row.get("longitude_wgs84")),
            "Description": _geovis_description(row),
            "Source": _geovis_source(row),
            "orig. Time": "",
            "Type": _clean(row.get("source_label")) or _clean(row.get("preset_name")) or "",
            "Confidence": "",                       # reserved for the assertion link (later)
            "Precision": _num_or_blank(row.get("horizontal_accuracy_m")),
        })
    frame = pd.DataFrame(rows, columns=list(_GEOVIS_COLUMNS))
    out = _out(ctx, "geovisualizer", "csv")
    frame.to_csv(out, index=False, quoting=csv.QUOTE_NONNUMERIC, encoding="utf-8-sig")
    return [out]
