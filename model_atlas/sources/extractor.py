"""Extractor — bound to a preset's data-gathering strategy.

Defines:    Extractor (Protocol), SingleSourceExtractor (the only one built), and a
            documented ScriptExtractor stub (the deferred ``extract: engine: python`` seam).
Used by:    the pipeline.
Depends on: readers (FormatReader registry), base (ExtractedData), presets.spec (InputSelector).

A SingleSourceExtractor resolves ONE role to one file, picks the FormatReader by the
selector's ``format``, reads, and builds ExtractedData — preserving every field's current
meaning and putting the reader's row-aligned ``recovery_state`` under ``enrichments``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Mapping, Protocol

from model_atlas.sources.base import ExtractedData
from model_atlas.sources.readers.base import get_reader

if TYPE_CHECKING:
    from model_atlas.presets.spec import InputSelector, PresetSpec
    from model_atlas.sources.container import SourceFile

log = logging.getLogger(__name__)


class Extractor(Protocol):
    def extract(
        self,
        file_to_selector: Mapping[str, tuple["SourceFile", "InputSelector"]],
        preset: "PresetSpec",
    ) -> ExtractedData: ...


def _source_label(file: "SourceFile", selector: "InputSelector") -> str:
    name = file.name
    if selector.format == "sqlite":
        return f"{name}::table={selector.table}" if selector.table else f"{name}::query"
    if selector.format == "excel":
        return f"{name}::sheet={selector.sheet}"
    return name


class SingleSourceExtractor:
    """One role -> one file -> one FormatReader. ~All presets use this implicitly."""

    def extract(
        self,
        file_to_selector: Mapping[str, tuple["SourceFile", "InputSelector"]],
        preset: "PresetSpec",
    ) -> ExtractedData:
        if len(file_to_selector) != 1:
            raise ValueError(
                f"SingleSourceExtractor expects exactly one role, got {sorted(file_to_selector)}"
            )
        (file, selector), = file_to_selector.values()
        reader = get_reader(selector.format)
        log.info(f"Extracting {file.full_logical_path} via {selector.format} reader")
        result = reader.read(file, selector.reader_params())
        return ExtractedData(
            dataframe=result.dataframe,
            source_file=_source_label(file, selector),
            source_original_path=str(file.logical_path),
            source_columns=result.source_columns,
            metadata=result.metadata,
            source_fingerprint=result.metadata.get("source_fingerprint"),
            origin=file,
            enrichments={"recovery_state": result.recovery_state},
        )


class ScriptExtractor:
    """DEFERRED — the ``extract: engine: python`` seam for cross-file/format joins.

    A future ScriptExtractor stages each named role's source and hands them to a
    separate, testable ``.py`` returning a DataFrame (e.g. plist username + sqlite
    messages). Reserved here; not implemented. See the input-selector redesign, Element 4.
    """

    def extract(self, file_to_selector, preset) -> ExtractedData:  # pragma: no cover
        raise NotImplementedError("multi-source python extract not yet implemented")
