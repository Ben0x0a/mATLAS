"""Source discovery, the Container model, format readers, and extraction.

The Container/SourceFile layer (``discover``), the per-format readers, and the
``SingleSourceExtractor`` together replace the old discovery + adapter registry. None of
these import ``presets.spec`` at module load, so ``presets.spec`` can safely import
``sources.pathmatch`` without a cycle.
"""
from __future__ import annotations

from model_atlas.sources.base import ExtractedData
from model_atlas.sources.container import (
    Container,
    FilesystemContainer,
    SourceFile,
    ZipContainer,
)
from model_atlas.sources.discover import discover
from model_atlas.sources.extractor import ScriptExtractor, SingleSourceExtractor
from model_atlas.sources.readers import FormatReader, ReadResult, get_reader

__all__ = [
    "discover",
    "ExtractedData",
    "Container",
    "FilesystemContainer",
    "ZipContainer",
    "SourceFile",
    "SingleSourceExtractor",
    "ScriptExtractor",
    "FormatReader",
    "ReadResult",
    "get_reader",
]
