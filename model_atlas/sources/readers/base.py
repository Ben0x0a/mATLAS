"""FormatReader protocol, ReadResult, the reader registry, and the recovery vocabulary.

Defines:    FormatReader (Protocol), ReadResult (uniform read output), register_reader /
            get_reader / registered_readers, and the controlled ``recovery_state`` vocab.
Used by:    the csv/excel/sqlite readers (register) and the extractor (dispatch).
Depends on: container/staging (SourceFile, StagedFile/StagedGroup), pandas.

A FormatReader is bound to a file FORMAT (not a container): it stages the file through
its Container, reads it into a DataFrame, and reports a row-aligned ``recovery_state``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd

if TYPE_CHECKING:
    from model_atlas.sources.container import SourceFile

log = logging.getLogger(__name__)

# Controlled vocabulary for the row-level recovery enrichment. Only live/wal/journal are
# produced in this task; freelist/unallocated are reserved for future carving readers.
RECOVERY_LIVE = "live"
RECOVERY_WAL = "wal"
RECOVERY_JOURNAL = "journal"
RECOVERY_FREELIST = "freelist"
RECOVERY_UNALLOCATED = "unallocated"
RECOVERY_STATES: frozenset[str] = frozenset(
    {RECOVERY_LIVE, RECOVERY_WAL, RECOVERY_JOURNAL, RECOVERY_FREELIST, RECOVERY_UNALLOCATED}
)


@dataclass(frozen=True)
class ReadResult:
    dataframe: pd.DataFrame
    source_columns: tuple[str, ...]
    recovery_state: tuple[str, ...]      # row-aligned with ``dataframe``
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class FormatReader(Protocol):
    format: str

    def read(self, file: "SourceFile", params: dict) -> ReadResult: ...
    def peek_columns(self, file: "SourceFile", selector: Any = None) -> set[str] | None: ...
    def list_subtables(self, file: "SourceFile") -> list[str]: ...


_READERS: dict[str, FormatReader] = {}


def register_reader(cls: type) -> type:
    instance = cls()
    _READERS.setdefault(instance.format, instance)
    log.debug(f"Registered format reader: {instance.format}")
    return cls


def get_reader(fmt: str) -> FormatReader:
    reader = _READERS.get(fmt)
    if reader is None:
        raise ValueError(f"no reader for format {fmt!r}")
    return reader


def registered_readers() -> tuple[FormatReader, ...]:
    return tuple(_READERS.values())
