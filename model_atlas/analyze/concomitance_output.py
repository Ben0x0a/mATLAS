"""Concomitance match output: the shared row shape + a streaming CSV sink.

Defines:    ``MatchRow`` (one result row: both sides' identity/where/when/traceability
            plus the four cross measures — the CSV column order below IS the contract),
            ``MatchSink`` (the ``open``/``write``/``close`` protocol a sink implements) and
            ``CsvMatchSink`` (a streaming, utf-8-sig CSV sink; never accumulates the full
            result in memory).
Used by:    ``model_atlas.app.concomitance_run`` (the only writer of ``MatchRow``
            instances) and, in T101b, ``launcher.cli``.
Depends on: the standard library only (``csv``, ``dataclasses``, ``pathlib``,
            ``typing.Protocol``) — no Qt, no sqlite3: a sink only ever sees
            already-resolved values, never a connection.
"""
from __future__ import annotations

import csv
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class MatchRow:
    """One concomitance finding: both sides (identity, where, when, traceability) plus
    the four cross measures. Field order IS the CSV column order (see ``CSV_HEADER``).
    """

    g1_identifier: str | None
    g1_identifier_type: str
    g1_entity_type: str
    g1_latitude: float
    g1_longitude: float
    g1_accuracy_m: float | None
    g1_time_start: str
    g1_time_end: str
    g1_observation_id: str
    g1_source: str | None

    g2_identifier: str | None
    g2_identifier_type: str
    g2_entity_type: str
    g2_latitude: float
    g2_longitude: float
    g2_accuracy_m: float | None
    g2_time_start: str
    g2_time_end: str
    g2_observation_id: str
    g2_source: str | None

    distance_m: float
    distance_within_accuracy: bool
    time_delta_s: float          # signed; negative = group 2 earlier
    time_overlap: bool


# The CSV header — the ONE place this column order is fixed (the task's contract table);
# neither shell re-derives it. Mirrors MatchRow's field order exactly.
CSV_HEADER: tuple[str, ...] = (
    "g1_identifier", "g1_identifier_type", "g1_entity_type",
    "g1_latitude", "g1_longitude", "g1_accuracy_m",
    "g1_time_start", "g1_time_end",
    "g1_observation_id", "g1_source",
    "g2_identifier", "g2_identifier_type", "g2_entity_type",
    "g2_latitude", "g2_longitude", "g2_accuracy_m",
    "g2_time_start", "g2_time_end",
    "g2_observation_id", "g2_source",
    "distance_m", "distance_within_accuracy", "time_delta_s", "time_overlap",
)


class MatchSink(Protocol):
    """Streaming write target for concomitance matches: open, write rows one at a time,
    close (idempotently)."""

    def open(self) -> None: ...
    def write(self, match: MatchRow) -> None: ...
    def close(self) -> None: ...


class CsvMatchSink:
    """Stream ``MatchRow`` instances to a CSV file, one row at a time.

    A run over two large groups can produce far more rows than either input, so rows are
    written as they arrive rather than buffered in a list. ``utf-8-sig`` (house rule) so
    Excel/Notepad auto-detect the encoding; the same request over the same case(s)
    produces a byte-identical file (determinism), since nothing here reorders rows.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle = None
        self._writer = None

    def open(self) -> None:
        self._handle = open(self._path, "w", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(CSV_HEADER)

    def write(self, match: MatchRow) -> None:
        # WHY the assert: a sink written to before open() (or after close()) is a caller
        # bug — writing silently to nothing would look like a successful run with a
        # missing/truncated file, the exact silent-loss class this tool exists to avoid.
        assert self._writer is not None, "CsvMatchSink.write() called before open()"
        self._writer.writerow(astuple(match))

    def close(self) -> None:
        # Idempotent: a cancelled run's cleanup path may call close() after it was
        # already closed on the normal path, and that must be a no-op, not an error.
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._writer = None
