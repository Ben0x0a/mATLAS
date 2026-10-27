"""The ``Reader`` protocol and ``RawRecord`` — the contract every format brick implements.

Defines:    ``RawRecord`` (one verbatim source record in its format's normative
            enumeration order, BEFORE any id is assigned) and the ``Reader`` Protocol
            (``format`` + ``subunits`` + ``iter_records`` + ``peek_fields``), per
            docs/refactor_plan.md §3.
Used by:    every concrete reader (csv_reader.py, excel_reader.py, sqlite_reader.py,
            xml_reader.py) and registry.py (the ``format -> Reader`` map); later, the
            discover/assemble bricks consume ``RawRecord`` to build the persisted
            ``core.vocabularies.RawRecord`` table row (which adds ``raw_record_id``,
            ``source_file_id`` and serialises ``fields`` into canonical-JSON ``raw_json``).
Depends on: ``model_atlas.core.vocabularies`` (``RecordState`` — live vs recovered;
            ``RecoverySource`` — the fine origin) and the standard library only.

WHY ``RawRecord`` is distinct from ``core.vocabularies.RawRecord``: the persisted table
row needs a ``source_file_id`` (the discover brick assigns it) and a ``raw_record_id`` derived
from it (uuid5) — neither exists yet at read time. This module's ``RawRecord`` carries
exactly what a reader can know on its own: the ``line_no`` in normative enumeration
order, the verbatim field values, and whether the row was live or recovered.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from model_atlas.core.vocabularies import RecordState, RecoverySource


@dataclass(frozen=True)
class RawRecord:
    """One verbatim source record, numbered per its format's normative enumeration rule
    (core/determinism.py).

    ``fields`` is a plain ``{column_name: value}`` mapping kept as source text/native
    SQLite types wherever float precision matters (canonical JSON rule 5) — never
    re-formatted. ``recovery_state`` defaults to LIVE; a sqlite reader overrides it to
    RECOVERED for WAL/journal/carved rows (rule 1). ``recovery_source`` is the fine
    companion — the exact origin (wal/journal/freelist/freespace) — and stays None for a
    live or intact row, which was recovered from nowhere. Non-sqlite readers therefore
    never set it: their rows are always live, so a populated cell always MEANS recovered.
    """

    line_no: int
    fields: dict[str, Any]
    # The STORED id, set only when this record was read back out of the case (the pipeline's
    # assemble pass). A reader that has just produced a record leaves it None: the row does
    # not exist yet, and the writer mints the id. Evidence-chain ids are random since
    # decision 50, so a later pass can no longer re-derive one — it must carry or query it.
    raw_record_id: str | None = None
    recovery_state: RecordState = RecordState.LIVE
    recovery_source: RecoverySource | None = None


@runtime_checkable
class Reader(Protocol):
    """One ingest brick per source FORMAT (not per container — discover resolves containers).

    ``source`` is the path to the opened unit (a real file on disk in this task; the
    discover/containers brick will resolve zip members etc. down to such a path before
    calling a reader). ``subunit`` names the reader-specific unit within the source (a
    sqlite table, an Excel sheet, an XML model type) — ``None``/``[]`` for formats with
    no subunit concept (csv).
    """

    format: str
    # True when a read is impossible without naming a sub-unit (a sqlite table, an Excel
    # sheet). WHY the writer needs it: when the sub-unit probe comes back empty it must tell
    # "a whole-file format with no sub-unit concept" (read it as one None unit) from "this
    # file could not be opened" (report it unread) — reading None on a sub-unit format is a
    # contract violation, not a fallback.
    requires_subunit: bool = False

    def subunits(self, source: Path) -> list[str]: ...

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]: ...  # normative enumeration order, verbatim

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None: ...
