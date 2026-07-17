"""Excel format reader (ingest brick): a workbook is one source; the sheet is the subunit.

Defines:    ``ExcelReader`` (format "excel").
Used by:    the ``format -> Reader`` registry (registered on import).
Depends on: ``model_atlas.core.determinism`` (``excel_line_no`` — rule 3),
            ``model_atlas.core.vocabularies`` (``RecordState``), ``model_atlas.ingest.
            readers.base``/``registry``, and ``pandas``/``openpyxl``.

Ported from ``sources/readers/excel_reader.py`` (same ``pandas.read_excel`` +
``openpyxl`` engine, same ``sheet``/``skip_rows``/``header_row`` params), plus this task's
two additions: R9 ``line_no`` (rule 3: the sheet row number itself) and verbatim values via
``dtype=str`` — this reader's "parse_float=str-style option": pandas skips numeric
inference entirely, so a genuinely textual cell survives byte-for-byte. A numeric Excel
cell is a documented, unavoidable exception: xlsx stores numbers as IEEE754, not text — a
cell displaying "46.193000" but typed as a number has already lost those trailing zeros in
the workbook's own binary storage before this reader ever opens it, so ``str(value)`` is
the closest available truth, not a loss this reader introduces.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd

from model_atlas.core.determinism import excel_line_no
from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader

log = logging.getLogger(__name__)


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
        df = pd.read_excel(
            source, sheet_name=subunit, skiprows=skip_rows, header=header_row,
            engine="openpyxl", dtype=str, na_filter=False,
        )
        # Rule 3: the sheet row number itself. Row 1 is skip_rows+header_row lines below the
        # top; the header consumes one more row; the first DATA row is one past that.
        first_data_row = skip_rows + header_row + 2
        for offset, row in enumerate(df.to_dict(orient="records")):
            line_no = excel_line_no(first_data_row + offset)
            yield RawRecord(line_no=line_no, fields=row, recovery_state=RecordState.LIVE)

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        if not subunit:
            return None
        try:
            frame = pd.read_excel(source, sheet_name=subunit, nrows=0, engine="openpyxl")
            return {str(c) for c in frame.columns}
        except Exception:  # noqa: BLE001
            log.debug(f"excel peek_fields failed for {source}", exc_info=True)
            return None
