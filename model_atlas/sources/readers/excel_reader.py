"""Excel format reader. A workbook is ONE file; the sheet comes from the selector."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from model_atlas.sources.container import SourceFile, staged_source
from model_atlas.sources.readers.base import RECOVERY_LIVE, ReadResult, register_reader
from model_atlas.sources.staging import STAGING_TIER, should_copy

log = logging.getLogger(__name__)


@register_reader
class ExcelReader:
    format = "excel"
    staging_mode = STAGING_TIER  # copy primary-tier evidence; read others in place

    def read(self, file: SourceFile, params: dict) -> ReadResult:
        sheet = params["sheet"]
        skip_rows = int(params.get("skip_rows", 0) or 0)
        header_row = int(params.get("header_row", 0) or 0)
        container = file.container
        with staged_source(
            container, file, copy=should_copy(self.staging_mode, params.get("_tier"))
        ) as staged:
            df = pd.read_excel(
                staged.path, sheet_name=sheet, skiprows=skip_rows,
                header=header_row, engine="openpyxl",
            )
        columns = tuple(str(c) for c in df.columns)
        return ReadResult(
            dataframe=df,
            source_fields=columns,
            recovery_state=(RECOVERY_LIVE,) * len(df),
            metadata={
                "format": "excel",
                "sheet": sheet,
                "source_fingerprint": staged.fingerprint,
                "row_count": len(df),
                "integrity": staged.integrity,
            },
        )

    def peek_fields(self, file: SourceFile, selector: Any = None) -> set[str] | None:
        sheet = getattr(selector, "sheet", None) if selector is not None else None
        if sheet is None:
            return None
        # Peeks are read-only and quick, so read in place where possible (a zip entry still
        # needs a real file, so staged_source copies it then); a probe makes no integrity
        # claim, hence finalize=False. The try wraps only the read (not the staging), so a
        # staging failure still propagates exactly as before.
        with staged_source(file.container, file, copy=False, finalize=False) as staged:
            try:
                frame = pd.read_excel(staged.path, sheet_name=sheet, nrows=0, engine="openpyxl")
                return {str(c) for c in frame.columns}
            except Exception:  # noqa: BLE001
                log.debug("excel peek_fields failed for %s", file.logical_path, exc_info=True)
                return None

    def list_subtables(self, file: SourceFile) -> list[str]:
        with staged_source(file.container, file, copy=False, finalize=False) as staged:
            try:
                return list(pd.ExcelFile(staged.path, engine="openpyxl").sheet_names)
            except Exception:  # noqa: BLE001
                log.debug(f"excel list_subtables failed for {file.logical_path}", exc_info=True)
                return []
