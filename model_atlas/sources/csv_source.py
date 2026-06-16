"""CSV extraction adapter."""
from __future__ import annotations

import logging

import pandas as pd

from model_atlas.integrity import sha256_file
from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.presets.spec import PresetSpec
from model_atlas.sources.base import ExtractedData
from model_atlas.sources.registry import register_adapter

log = logging.getLogger(__name__)


def extract_csv(element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
    config = preset.extract.get("csv", {}) if isinstance(preset.extract, dict) else {}
    delimiter = config.get("delimiter", ",")
    encoding = config.get("encoding", "utf-8")
    skip_rows = int(config.get("skip_rows", 0) or 0)
    header_row = int(config.get("header_row", 0) or 0)
    log.info("Reading CSV source: %s", element.path)
    log.debug(
        "CSV extraction config: path=%s delimiter=%r encoding=%s skip_rows=%d header_row=%d preset=%s",
        element.path,
        delimiter,
        encoding,
        skip_rows,
        header_row,
        preset.name,
    )
    hash_before = sha256_file(element.path)
    df = pd.read_csv(
        element.path,
        sep=delimiter,
        encoding=encoding,
        skiprows=skip_rows,
        header=header_row,
    )
    hash_after = sha256_file(element.path)
    log.debug(
        "CSV extraction complete: path=%s rows=%d columns=%s integrity_ok=%s",
        element.path,
        len(df),
        [str(c) for c in df.columns],
        hash_before == hash_after,
    )
    return ExtractedData(
        element=element,
        dataframe=df,
        source_file=element.source_file,
        source_original_path=element.source_original_path,
        source_columns=tuple(str(c) for c in df.columns),
        metadata={
            "source_type": "csv",
            "path": str(element.path),
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
class CsvAdapter:
    """Source adapter for CSV files."""

    name = "csv"

    def can_handle(self, element: DiscoveredElement) -> bool:
        return element.source_type == ElementType.CSV

    def extract(self, element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
        return extract_csv(element, preset)
