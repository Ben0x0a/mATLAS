"""Format readers: one per file format, selected by the preset/discover-declared ``format``.

Importing this package registers the built-in readers (csv, excel, sqlite, xml) so
``registry.get_reader`` can look one up by format string.
"""
from __future__ import annotations

from model_atlas.ingest.readers.base import RawRecord, Reader
from model_atlas.ingest.readers.registry import get_reader, register_reader, registered_readers

# Imported for their registration side effect. xml_reader registers XmlReader but does NOT
# import lxml/ufdr_parser at module load — those arrive only when an XML source is read.
from model_atlas.ingest.readers import (  # noqa: E402,F401
    csv_reader,
    excel_reader,
    sqlite_reader,
    xml_reader,
)

__all__ = [
    "RawRecord",
    "Reader",
    "get_reader",
    "register_reader",
    "registered_readers",
]
