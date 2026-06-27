"""Excel format reader. A workbook is ONE file; the sheet comes from the selector."""
from __future__ import annotations

import logging
import shutil
from typing import Any

import pandas as pd

from model_atlas.sources.container import SourceFile, acquire_source
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
        staged = acquire_source(container, file, copy=should_copy(self.staging_mode, params.get("_tier")))
        try:
            df = pd.read_excel(
                staged.path, sheet_name=sheet, skiprows=skip_rows,
                header=header_row, engine="openpyxl",
            )
            container.finalize(staged)
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)
        columns = tuple(str(c) for c in df.columns)
        return ReadResult(
            dataframe=df,
            source_columns=columns,
            recovery_state=(RECOVERY_LIVE,) * len(df),
            metadata={
                "format": "excel",
                "sheet": sheet,
                "source_fingerprint": staged.fingerprint,
                "row_count": len(df),
                "integrity": staged.integrity,
            },
        )

    def _staged_path(self, file: SourceFile):
        # Peeks are read-only and quick, so read in place where possible (a zip entry still
        # needs a real file, so acquire_source copies it then).
        container = file.container
        staged = acquire_source(container, file, copy=False)
        return container, staged

    def peek_columns(self, file: SourceFile, selector: Any = None) -> set[str] | None:
        sheet = getattr(selector, "sheet", None) if selector is not None else None
        if sheet is None:
            return None
        container, staged = self._staged_path(file)
        try:
            frame = pd.read_excel(staged.path, sheet_name=sheet, nrows=0, engine="openpyxl")
            return {str(c) for c in frame.columns}
        except Exception:  # noqa: BLE001
            log.debug(f"excel peek_columns failed for {file.logical_path}", exc_info=True)
            return None
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)

    def list_subtables(self, file: SourceFile) -> list[str]:
        container, staged = self._staged_path(file)
        try:
            return list(pd.ExcelFile(staged.path, engine="openpyxl").sheet_names)
        except Exception:  # noqa: BLE001
            log.debug(f"excel list_subtables failed for {file.logical_path}", exc_info=True)
            return []
        finally:
            if staged.temp_dir is not None:
                shutil.rmtree(staged.temp_dir, ignore_errors=True)
