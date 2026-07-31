"""Excel format reader (ingest brick): a workbook is one source; the sheet is the subunit.

Defines:    ``ExcelReader`` (format "excel").
Used by:    the ``format -> Reader`` registry (registered on import).
Depends on: ``model_atlas.core.determinism`` (``excel_line_no`` — rule 3),
            ``model_atlas.core.vocabularies`` (``RecordState``), ``model_atlas.ingest.
            readers.base``/``registry``, and ``pandas``/``openpyxl``.

Ported from ``sources/readers/excel_reader.py`` (same ``sheet``/``skip_rows``/``header_row``
params). ``subunits`` / ``peek_fields`` still probe via ``pandas.ExcelFile`` /
``pandas.read_excel``, but ``iter_records`` reads the grid DIRECTLY with ``openpyxl`` so the
normative ``line_no`` (rule 3) is the row's own 1-based sheet position — not an enumerate
offset over a pandas frame that could silently collapse a blank row and shift every later
line_no. Values are kept verbatim as ``str(cell)``: a numeric Excel cell is a documented,
unavoidable exception — xlsx stores numbers as IEEE754, not text — a cell displaying
"46.193000" but typed as a number has already lost those trailing zeros in the workbook's
own binary storage before this reader ever opens it, so ``str(value)`` is the closest
available truth, not a loss this reader introduces.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd

from model_atlas.core.determinism import excel_line_no
from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader

log = logging.getLogger(__name__)


def _cell_text(value: object) -> str:
    """Verbatim text of one data cell: an empty cell is ``""`` (matching this reader's former
    ``pandas.read_excel(dtype=str, na_filter=False)`` path), any other value its ``str()`` — a
    numeric/date cell has already been stored lossily as IEEE754 in the workbook binary, so
    ``str()`` is the closest available truth, not a loss this reader introduces."""
    return "" if value is None else str(value)


@register_reader
class ExcelReader:
    format = "excel"

    def subunits(self, source: Path) -> list[str]:
        try:
            return list(pd.ExcelFile(source, engine="openpyxl").sheet_names)
        except Exception:  # noqa: BLE001 - a probe failure must never abort matching
            log.debug(f"excel subunits failed for {source}", exc_info=True)
            return []

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]:
        if not subunit:
            raise ValueError("an excel read needs a sheet name (subunit)")
        params = params or {}
        skip_rows = int(params.get("skip_rows", 0) or 0)
        header_row = int(params.get("header_row", 0) or 0)
        # Rule 3: line_no is the TRUE 1-based sheet row. HOW: read the grid directly with
        # openpyxl and derive each record's line_no from the row's OWN position, rather than
        # an enumerate offset over a pandas frame. WHY: a pandas frame can silently collapse a
        # blank row, which would shift every later line_no; reading the sheet keeps each row's
        # real number by construction, so a blank row keeps its number and shifts nothing.
        workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
        try:
            worksheet = workbook[subunit]
            # The header sits skip_rows lines below the top, then header_row further down
            # (both 0-based params): 1-based sheet row skip_rows + header_row + 1.
            header_sheet_row = skip_rows + header_row + 1
            grid = worksheet.iter_rows(min_row=header_sheet_row, values_only=True)
            header_cells = next(grid, None)
            if header_cells is None:
                return
            # An unnamed (empty) header cell has no column to key under, so its cells are
            # dropped — the same shape the pandas header path produced.
            header = [None if cell is None else str(cell) for cell in header_cells]
            # Buffer rows so trailing all-empty rows (which carry no record — the pandas path
            # dropped these too) fall off the end WITHOUT dropping a blank row that sits
            # BETWEEN two real rows (that one keeps its line_no).
            pending: list[tuple[int, dict[str, str]]] = []
            for offset, cells in enumerate(grid):
                sheet_row = header_sheet_row + 1 + offset
                fields = {
                    name: (_cell_text(cells[i]) if i < len(cells) else "")
                    for i, name in enumerate(header) if name is not None
                }
                pending.append((sheet_row, fields))
            while pending and all(value == "" for value in pending[-1][1].values()):
                pending.pop()
            for sheet_row, fields in pending:
                yield RawRecord(line_no=excel_line_no(sheet_row), fields=fields,
                                recovery_state=RecordState.LIVE)
        finally:
            workbook.close()

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        if not subunit:
            return None
        try:
            frame = pd.read_excel(source, sheet_name=subunit, nrows=0, engine="openpyxl")
            return {str(c) for c in frame.columns}
        except Exception:  # noqa: BLE001
            log.debug(f"excel peek_fields failed for {source}", exc_info=True)
            return None
