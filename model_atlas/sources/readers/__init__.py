"""Format readers: one per file format, selected by the magic-detected ``format``.

Importing this package registers the built-in readers (csv, excel, sqlite) so the
extractor can look one up by format string.
"""
from __future__ import annotations

from model_atlas.sources.readers.base import (
    RECOVERY_STATES,
    FormatReader,
    ReadResult,
    get_reader,
    registered_readers,
)

# Imported for their registration side effect.
from model_atlas.sources.readers import (  # noqa: E402,F401
    csv_reader,
    excel_reader,
    sqlite_reader,
)

__all__ = [
    "RECOVERY_STATES",
    "FormatReader",
    "ReadResult",
    "get_reader",
    "registered_readers",
]
