"""KML export brick: Google-Earth placemarks for the flat-projected rows that carry a
coordinate (T51 — ports ``model_atlas/exporters/builtin.py``'s ``export_kml``).

Defines:    KmlExporter (the ``Exporter`` protocol brick, ``format = "kml"``),
            registered in model_atlas.export.registry at import time.
Used by:    model_atlas.export.registry (registration) and tests/export/test_kml.py.
Depends on: model_atlas.core.projection (the flat query), model_atlas.export.csv_flat
            (the ``Exporter`` protocol / ``ExportContext``), model_atlas.export._shared
            (value helpers + partitioning), the standard library's ``xml.etree``.

Query + serialiser (T51 scope)
-------------------------------
The data access is ONE query — ``core.projection.project`` (the same flat frame the
CSV exporter reads); this module's only job is picking the coordinate-bearing rows
and shaping them into KML Placemarks, faithfully reproducing the old exporter's XML
shape (name = linked_entity, TimeStamp/when = the lower-bound local ISO instant,
Point/coordinates = "lon,lat"). The old single ``utc_offset_hours`` column split into
``utc_offset_hours_lower`` / ``_upper`` in the relational model (finding F2); the
placemark's one timestamp uses the LOWER bound + ITS OWN offset — for an instant
assertion (the common case) lower == upper, so this is exactly what the old single-
offset model always meant.

Encoding
--------
Plain ``utf-8`` (not ``utf-8-sig``): KML is XML, and ``ElementTree.write(...,
xml_declaration=True)`` emits ``<?xml version='1.0' encoding='utf-8'?>`` — a BOM ahead
of that declaration is invalid XML and several KML consumers (incl. Google Earth)
reject it outright. This matches the old exporter's encoding choice exactly.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, ElementTree, SubElement

import pandas as pd

from model_atlas.core.projection import project
from model_atlas.core.store import RunStore
from model_atlas.export._shared import UNIX_EPOCH, as_float, clean, is_blank, partitions
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter


def _local_iso(unix_us: Any, offset_hours: Any) -> str:
    """ISO-8601 local timestamp: the absolute UTC instant shifted by the row's own
    offset (so the wall-clock and the +HH:MM suffix match). '' when there is no
    timestamp — ported verbatim from the old exporter's ``_local_iso``.
    """
    if is_blank(unix_us):
        return ""
    try:
        microseconds = int(unix_us)
    except (TypeError, ValueError):
        return ""
    tz = timezone(timedelta(hours=as_float(offset_hours) or 0.0))
    return (UNIX_EPOCH + timedelta(microseconds=microseconds)).astimezone(tz).isoformat()


class KmlExporter:
    """Placemarks with timestamps for located rows (``format = "kml"``)."""

    format = "kml"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        frame = project(
            store.connection, ranking_method=ctx.ranking_method, include_source_columns=False)
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        return [
            self._write(part, ctx.output_dir / f"{stem}.points.kml")
            for stem, part in partitions(frame, ctx)
        ]

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        kml = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
        document = SubElement(kml, "Document")
        for row in frame.to_dict(orient="records"):
            lat, lon = as_float(row.get("latitude_wgs84")), as_float(row.get("longitude_wgs84"))
            if lat is None or lon is None:
                continue   # a placemark needs a coordinate; rows without one are omitted
            placemark = SubElement(document, "Placemark")
            SubElement(placemark, "name").text = clean(row.get("linked_entity")) or ""
            when = _local_iso(row.get("time_lower_unix_utc_us"), row.get("utc_offset_hours_lower"))
            if when:
                SubElement(SubElement(placemark, "TimeStamp"), "when").text = when
            SubElement(SubElement(placemark, "Point"), "coordinates").text = f"{lon},{lat}"
        ElementTree(kml).write(path, encoding="utf-8", xml_declaration=True)
        return path


register_exporter(
    "kml", "KML", KmlExporter(),
    "Google-Earth placemarks with timestamps for located rows.",
)
