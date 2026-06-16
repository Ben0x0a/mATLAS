"""Typed dataclasses shared across the pipeline.

Defines:    ElementType and DiscoveredElement — the discovery output consumed by the
            source-adapter registry and the preset matcher.
Used by:    sources (discovery + adapters), presets.matcher, pipeline.
Depends on: standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ElementType(str, Enum):
    CSV = "csv"
    EXCEL = "excel"
    SQLITE = "sqlite"


@dataclass(frozen=True)
class DiscoveredElement:
    """One processable input unit: CSV file, Excel sheet, or SQLite table."""

    source_type: ElementType
    path: Path
    source_file: str
    source_original_path: str
    logical_name: str
    table_name: str | None = None
    sheet_name: str | None = None
    query_name: str | None = None
    preview_supported: bool = True
