"""Excel extraction adapter."""
from __future__ import annotations

import logging

import pandas as pd

from model_atlas.integrity import sha256_file
from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.presets.spec import PresetSpec
from model_atlas.sources.base import ExtractedData
from model_atlas.sources.registry import register_adapter

log = logging.getLogger(__name__)


def extract_excel(element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
    config = preset.extract.get("excel", {}) if isinstance(preset.extract, dict) else {}
    sheet_name = config.get("sheet_name") or element.sheet_name
    skip_rows = int(config.get("skip_rows", 0) or 0)
    header_row = int(config.get("header_row", 0) or 0)
    log.info("Reading Excel source: %s sheet=%s", element.path, sheet_name)
    log.debug(
        "Excel extraction config: path=%s sheet=%s skip_rows=%d header_row=%d preset=%s",
        element.path,
        sheet_name,
        skip_rows,
        header_row,
        preset.name,
    )
    hash_before = sha256_file(element.path)
    df = pd.read_excel(
        element.path,
        sheet_name=sheet_name,
        skiprows=skip_rows,
        header=header_row,
        engine="openpyxl",
    )
    hash_after = sha256_file(element.path)
    source_file = f"{element.path.name}::sheet={sheet_name}"
    log.debug(
        "Excel extraction complete: source_file=%s rows=%d columns=%s integrity_ok=%s",
        source_file,
        len(df),
        [str(c) for c in df.columns],
        hash_before == hash_after,
    )
    return ExtractedData(
        element=element,
        dataframe=df,
        source_file=source_file,
        source_original_path=element.source_original_path,
        source_columns=tuple(str(c) for c in df.columns),
        metadata={
            "source_type": "excel",
            "path": str(element.path),
            "sheet_name": sheet_name,
            "row_count": len(df),
            "integrity": {
                "mode": "full",
                "ok": hash_before == hash_after,
                "source_hash_before": hash_before,
                "verification_after": {"sha256": hash_after},
            },
        },
    )


@register_adapter
class ExcelAdapter:
    """Source adapter for Excel worksheets."""

    name = "excel"

    def can_handle(self, element: DiscoveredElement) -> bool:
        return element.source_type == ElementType.EXCEL

    def extract(self, element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
        return extract_excel(element, preset)
