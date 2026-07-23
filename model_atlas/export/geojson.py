"""GeoJSON export brick: a Point FeatureCollection over the flat-projected rows that
carry coordinates (T51 — ports ``exporters/builtin.py``'s
``export_geojson``).

Defines:    GeojsonExporter (the ``Exporter`` protocol brick, ``format = "geojson"``),
            registered in model_atlas.export.registry at import time.
Used by:    model_atlas.export.registry (registration) and tests/export/test_geojson.py.
Depends on: model_atlas.core.projection (the flat query), model_atlas.export.csv_flat
            (the ``Exporter`` protocol / ``ExportContext``), model_atlas.export._shared
            (value helpers + partitioning).

Query + serialiser (T51 scope)
-------------------------------
The data access is ONE query — ``core.projection.project``. Every canonical column
except the two coordinates becomes a GeoJSON ``properties`` entry (a blank/NaN/NA cell
becomes ``null``, matching the old exporter), so a GIS consumer sees the same
attribute set the old exporter exposed (extended with the relational model's new
canonical columns — R6's ``spatial_referential`` / ``locates`` / ``spatial_method`` /
``temporal_method`` among them).

Encoding — a documented DEVIATION from the old exporter
-----------------------------------------------------------
The old exporter wrote GeoJSON as ``utf-8-sig`` (a BOM). RFC 7946 §5 is explicit:
"implementations MUST NOT add a byte order mark (BOM) to the beginning of a GeoJSON
text" — a BOM makes some JSON/GIS parsers choke on the file. This brick therefore
writes plain ``utf-8`` (no BOM), correcting that old-exporter defect rather than
porting it faithfully; the house "utf-8-sig for file output" rule explicitly carves
out this exception for formats whose consumers reject a BOM (GeoJSON is one).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from model_atlas.core.projection import project
from model_atlas.core.store import RunStore
from model_atlas.export._shared import as_float, is_blank
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter

_LATITUDE_COLNAME = "latitude_wgs84"
_LONGITUDE_COLNAME = "longitude_wgs84"


class GeojsonExporter:
    """Point FeatureCollection of the rows that carry coordinates (``format = "geojson"``)."""

    format = "geojson"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        frame = project(
            store.connection, ranking_method=ctx.ranking_method, include_source_columns=False)
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        return [self._write(frame, ctx.output_dir / f"{ctx.base_name}.points.geojson")]

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        features: list[dict] = []
        for row in frame.to_dict(orient="records"):
            lat, lon = as_float(row.get(_LATITUDE_COLNAME)), as_float(row.get(_LONGITUDE_COLNAME))
            if lat is None or lon is None:
                continue   # a point feature needs a coordinate; rows without one are omitted
            properties = {
                key: (None if is_blank(val) else val)
                for key, val in row.items() if key not in (_LATITUDE_COLNAME, _LONGITUDE_COLNAME)
            }
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": properties,
            })
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": features},
                       indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",   # RFC 7946 §5: no BOM (see module docstring)
        )
        return path


register_exporter(
    "geojson", "GeoJSON", GeojsonExporter(),
    "Point FeatureCollection of the rows that carry coordinates.",
)
