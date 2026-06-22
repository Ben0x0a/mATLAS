"""The ExtractedData contract handed from the extractor to the transform engine.

Defines:    ExtractedData — the frame plus its provenance, integrity metadata, content
            fingerprint, and the new row-aligned ``enrichments`` (e.g. recovery_state).
Used by:    the Extractor (produces it) and the pipeline (consumes it).
Depends on: container (SourceFile origin), pandas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from model_atlas.sources.container import SourceFile


@dataclass(frozen=True)
class ExtractedData:
    dataframe: pd.DataFrame
    source_file: str                       # human label, e.g. "Cache.sqlite::table=ZRTCLLOCATIONMO"
    source_original_path: str              # the inner-container logical path (raw_source_path default)
    source_columns: tuple[str, ...]
    metadata: dict[str, Any]
    # Content hash of the specific source (the DB / CSV / sheet bytes, not the wrapping
    # archive). Scopes the generated source_record_uid so it is globally unique and portable.
    source_fingerprint: str | None = None
    origin: "SourceFile | None" = None
    # Row-aligned engine-computed enrichments (length == len(dataframe)). First key:
    # ``recovery_state``. Surfaced here only; the transform/export side is untouched.
    enrichments: dict[str, tuple[Any, ...]] = field(default_factory=dict)
