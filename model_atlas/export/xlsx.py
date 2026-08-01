"""XLSX export brick: the flat-projected rows as an Excel worksheet (ports
``exporters/builtin.py``'s ``export_xlsx``).

Defines:    XlsxExporter (the ``Exporter`` protocol brick, ``format = "xlsx"``),
            registered in model_atlas.export.registry at import time.
Used by:    model_atlas.export.registry (registration) and tests/export/test_xlsx.py.
Depends on: model_atlas.core.flat_view (the flat query), model_atlas.export.csv_flat
            (the ``Exporter`` protocol / ``ExportContext``), model_atlas.export._shared
            (the shared formula-injection rule), pandas + openpyxl (already a
            project dependency).

Query + serialiser
--------------------
The data access is ONE query — ``core.flat_view.build_flat_view`` — the same flat frame the
CSV/KML exporters read. It is written with openpyxl directly (NOT ``DataFrame.to_excel``)
because ``to_excel`` routes a nullable ``Int64`` column through float64 before writing —
see below.

Why the cells are typed by hand (two forensic-integrity fixes)
--------------------------------------------------------------
1. Int64 precision. ``DataFrame.to_excel`` casts every nullable ``Int64`` column to float64,
   and openpyxl in turn serialises any integer beyond float64's 53-bit exact range
   (``2**53``) into a float literal in the sheet XML — silently corrupting ``s2_cell_id``
   (a signed int64 S2 leaf cell, ~2**60) into a WRONG value while the CSV/GeoJSON carry the
   correct one. So each ``Int64`` cell is written as an exact Python ``int`` when it fits
   float64's safe range, and as TEXT (the decimal string, byte-exact) when it does not.
2. Formula injection. openpyxl treats a string beginning ``=``/``+``/``-``/``@`` as a LIVE
   formula (data-type 'f'), which the analyst's Excel/Sheets executes on open. A trigger
   cell (``_shared.is_formula_trigger`` — the ONE rule the CSV writers also use) is written
   as text (data-type 's') keeping the value byte-exact, so it re-imports to the original
   string but never evaluates.

Encoding
--------
Binary spreadsheet format (openpyxl/OOXML) — no text encoding decision applies.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell.cell import TYPE_STRING

from model_atlas.core.flat_view import build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export._shared import is_blank, is_formula_trigger
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter

# float64 represents every integer with magnitude <= 2**53 exactly; beyond that consecutive
# integers collapse onto the same float, so such a value must be written as text to survive.
_FLOAT64_SAFE_INT = 2**53


class XlsxExporter:
    """Assertion rows as an Excel worksheet (``format = "xlsx"``)."""

    format = "xlsx"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        frame = build_flat_view(
            store.connection, ranking_method=ctx.ranking_method, include_source_columns=False)
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.output_dir / f"{ctx.base_name}.assertions.xlsx"
        return [self._write(frame, path)]

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        """Write ``frame`` as an .xlsx, typing each cell by hand (see the module docstring).

        HOW: an integer-dtype column is written as an exact int (or text when it exceeds
        float64's safe range); a formula-trigger string is forced to a text cell; every
        other cell is written as-is, with a blank/NA cell left empty.
        """
        integer_columns = {
            name for name in frame.columns if isinstance(frame[name].dtype, pd.Int64Dtype)
        }
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(list(frame.columns))   # header at row 1
        # Row 1 is the header; data rows follow. An explicit counter (not worksheet.max_row)
        # is used because a fully blank data row would not advance max_row, mis-aligning the
        # next row's cells onto it.
        row_index = 1
        for record in frame.to_dict(orient="records"):
            row_index += 1
            for column_index, name in enumerate(frame.columns, start=1):
                self._set_cell(
                    worksheet.cell(row=row_index, column=column_index),
                    record[name], name in integer_columns)
        workbook.save(path)
        return path

    @staticmethod
    def _set_cell(cell: Any, value: Any, is_integer_column: bool) -> None:
        """Write one value into ``cell`` with the correct, lossless, inert type."""
        if is_blank(value):
            return   # leave the cell empty (a corner-less assertion's missing Int64/text)
        if is_integer_column:
            number = int(value)
            # An int within float64's exact range writes as a clean number; a larger one
            # (s2_cell_id) would be floated by openpyxl, so it is written as byte-exact text.
            cell.value = number if abs(number) <= _FLOAT64_SAFE_INT else str(number)
            return
        if is_formula_trigger(value):
            # Force text (data-type 's'): the value stays byte-exact (re-imports to the exact
            # original string) but is never evaluated as a formula on open.
            cell.value = value
            cell.data_type = TYPE_STRING
            return
        cell.value = value


register_exporter(
    "xlsx", "Excel (.xlsx)", XlsxExporter(),
    "Assertion rows as an Excel worksheet.",
)
