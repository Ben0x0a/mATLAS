"""Shared source adapter types.

Defines:    ExtractedData (the extractor output contract) and the SourceAdapter
            protocol that every format adapter implements.
Used by:    sources.registry, the csv/excel/sqlite adapters, and the pipeline.
Depends on: models (DiscoveredElement); presets.spec only for type checking.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd

from model_atlas.models import DiscoveredElement

if TYPE_CHECKING:
    from model_atlas.presets.spec import PresetSpec


@dataclass(frozen=True)
class ExtractedData:
    element: DiscoveredElement
    dataframe: pd.DataFrame
    source_file: str
    source_original_path: str
    source_columns: tuple[str, ...]
    metadata: dict[str, Any]
    # Content hash of the specific source artefact (the DB / CSV / sheet bytes, not the
    # wrapping archive). Scopes the generated record_uid so it is globally unique and
    # portable: two acquisitions with the same path + row differ here. None if unhashed.
    source_fingerprint: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """A pluggable extractor for one source format.

    Implementations register with ``@register_adapter`` and are discovered by the
    pipeline through the registry, so a new format is added by dropping in a module
    — the core dispatch never changes.
    """

    name: str

    def can_handle(self, element: DiscoveredElement) -> bool:
        """Return True if this adapter can extract the given discovered element."""
        ...

    def extract(self, element: DiscoveredElement, preset: "PresetSpec") -> ExtractedData:
        """Read the element into an ExtractedData frame plus integrity metadata."""
        ...
