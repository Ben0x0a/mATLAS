"""The ``Reader`` protocol and ``RawRecord`` — the contract every format brick implements.

Defines:    ``RawRecord`` (one verbatim source record in R9 enumeration order, BEFORE any
            id is assigned) and the ``Reader`` Protocol (``format`` + ``subunits`` +
            ``iter_records`` + ``peek_fields``), per docs/refactor_plan.md §3.
Used by:    every concrete reader (csv_reader.py, excel_reader.py, sqlite_reader.py,
            xml_reader.py) and registry.py (the ``format -> Reader`` map); later, T21's
            discover/assemble bricks consume ``RawRecord`` to build the persisted
            ``core.vocabularies.RawRecord`` table row (which adds ``raw_record_id``,
            ``source_id`` and serialises ``fields`` into canonical-JSON ``raw_json``).
Depends on: ``model_atlas.core.vocabularies`` (``RecordState`` — live vs recovered) and
            the standard library only.

WHY ``RawRecord`` is distinct from ``core.vocabularies.RawRecord``: the persisted table
row needs a ``source_id`` (T21's discover brick assigns it) and a ``raw_record_id`` derived
from it (uuid5) — neither exists yet at read time. This module's ``RawRecord`` carries
exactly what a reader can know on its own: the R9 ``line_no``, the verbatim field values,
and whether the row was live or recovered.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from model_atlas.core.vocabularies import RecordState


@dataclass(frozen=True)
class RawRecord:
    """One verbatim source record, numbered per its format's R9 rule (core/determinism.py).

    ``fields`` is a plain ``{column_name: value}`` mapping kept as source text/native
    SQLite types wherever float precision matters (canonical JSON rule 5) — never
    re-formatted. ``recovery_state`` defaults to LIVE; a sqlite reader overrides it to
    RECOVERED for WAL/journal-only rows (rule 1).
    """

    line_no: int
    fields: dict[str, Any]
    recovery_state: RecordState = RecordState.LIVE


@runtime_checkable
class Reader(Protocol):
    """One ingest brick per source FORMAT (not per container — T21 resolves containers).

    ``source`` is the path to the opened unit (a real file on disk in this task; T21's
    discover/containers brick will resolve zip members etc. down to such a path before
    calling a reader). ``subunit`` names the reader-specific unit within the source (a
    sqlite table, an Excel sheet, an XML model type) — ``None``/``[]`` for formats with
    no subunit concept (csv).
    """

    format: str

    def subunits(self, source: Path) -> list[str]: ...

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]: ...  # R9 order, verbatim

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None: ...
