"""CSV format reader."""
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
class CsvReader:
    format = "csv"
    staging_mode = STAGING_TIER  # copy primary-tier evidence; read others in place

    def read(self, file: SourceFile, params: dict) -> ReadResult:
        delimiter = params.get("delimiter", ",")
        encoding = params.get("encoding", "utf-8")
        skip_rows = int(params.get("skip_rows", 0) or 0)
        header_row = int(params.get("header_row", 0) or 0)
        container = file.container
        staged = acquire_source(container, file, copy=should_copy(self.staging_mode, params.get("_tier")))
        try:
            df = pd.read_csv(
                staged.path, sep=delimiter, encoding=encoding,
                skiprows=skip_rows, header=header_row,
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
                "format": "csv",
                "source_fingerprint": staged.fingerprint,
                "row_count": len(df),
                "integrity": staged.integrity,
            },
        )

    def peek_columns(self, file: SourceFile, selector: Any = None) -> set[str] | None:
        delimiter = ","
        if selector is not None:
            delimiter = (selector.read or {}).get("delimiter", ",")
        try:
            with file.container.open(file) as raw:
                header = raw.readline().decode("utf-8-sig", errors="replace").rstrip("\r\n")
            return set(header.split(delimiter)) if header else set()
        except Exception:  # noqa: BLE001 - a peek failure must never abort matching
            log.debug(f"csv peek_columns failed for {file.logical_path}", exc_info=True)
            return None

    def list_subtables(self, file: SourceFile) -> list[str]:
        return []
