"""XLSX export brick: the flat-projected rows as an Excel worksheet (T51 — ports
``exporters/builtin.py``'s ``export_xlsx``).

Defines:    XlsxExporter (the ``Exporter`` protocol brick, ``format = "xlsx"``),
            registered in model_atlas.export.registry at import time.
Used by:    model_atlas.export.registry (registration) and tests/export/test_xlsx.py.
Depends on: model_atlas.core.projection (the flat query), model_atlas.export.csv_flat
            (the ``Exporter`` protocol / ``ExportContext``), model_atlas.export._shared
            (partitioning), pandas + openpyxl (already a project dependency).

Query + serialiser (T51 scope)
-------------------------------
The data access is ONE query — ``core.projection.project`` — written straight to a
worksheet via ``DataFrame.to_excel``, exactly as the old exporter did; the only change
is that the frame now comes from a SELECT over the store rather than a pre-built
dataframe handed in by the pipeline.

Encoding
--------
Binary spreadsheet format (openpyxl/OOXML) — no text encoding decision applies.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.core.projection import project
from model_atlas.core.store import RunStore
from model_atlas.export._shared import partitions
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter


class XlsxExporter:
    """Assertion rows as an Excel worksheet (``format = "xlsx"``)."""

    format = "xlsx"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        frame = project(
            store.connection, ranking_method=ctx.ranking_method, include_source_columns=False)
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for stem, part in partitions(frame, ctx):
            path = ctx.output_dir / f"{stem}.assertions.xlsx"
            part.to_excel(path, index=False)   # openpyxl engine (already a dependency)
            written.append(path)
        return written


register_exporter(
    "xlsx", "Excel (.xlsx)", XlsxExporter(),
    "Assertion rows as an Excel worksheet.",
)
