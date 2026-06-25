"""Buffer flattened records to disk and flush them to per-(level, type) CSVs.

Defines:    CsvSink — accumulates FlatRecords in a SQLite spill table, tracks each
            (level, model_type)'s column order, and writes one CSV per group with the
            source columns joined in; GroupSpec (one write job) and write_group (the
            reusable per-group writer shared by the serial flush and the parallel workers).
Used by:    dump (the orchestrator) and parallel (the flush workers).
Depends on: const (column/level names), source_lookup (SourceIndex/resolve), models
            (FlatRecord), sqlite3, csv.

WHY a SQLite spill: record field sets vary per row, so the full CSV header for a type is
only known once every row is seen. Holding all rows in RAM will not scale to a 32 GB
report, so rows are staged on disk (with their model id) and the header is built from a
small in-memory per-type column-order map; at flush each row is joined to its source and
written. Output is UTF-8 with BOM so Excel detects accented text correctly.
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ufdr_parser.const import (
    COL_MAIN_UUID,
    COL_SUB_UUID,
    COL_UUID,
    LEVEL_FILE_PREFIX,
    LEVEL_TOP,
    SOURCE_COLUMNS,
)
from ufdr_parser.models import FlatRecord
from ufdr_parser.source_lookup import SourceIndex

log = logging.getLogger(__name__)

_BATCH = 5000


class CsvSink:
    """Disk-buffered, per-(level, type) CSV writer with source-column join."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._pending: list[tuple[int, str, str | None, str | None, str]] = []
        # (level, model_type) -> ordered field names (dict used as an ordered set).
        self._field_order: dict[tuple[int, str], dict[str, None]] = {}
        conn.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, level INTEGER, model_type TEXT, "
            "id TEXT, parent_id TEXT, fields_json TEXT)"
        )
        # Group lookups (flush + parallel merge) filter by (level, model_type); index it so
        # a flush does not full-scan the spill once per output file.
        conn.execute("CREATE INDEX IF NOT EXISTS ix_records_group ON records(level, model_type)")

    def add(self, record: FlatRecord) -> None:
        key = (record.level, record.model_type)
        order = self._field_order.setdefault(key, {})
        for name in record.fields:
            order.setdefault(name, None)
        self._pending.append(
            (
                record.level,
                record.model_type,
                record.model_id,
                record.parent_id,
                json.dumps(record.fields, ensure_ascii=False),
            )
        )
        if len(self._pending) >= _BATCH:
            self._drain()

    def _drain(self) -> None:
        if not self._pending:
            return
        self._conn.executemany(
            "INSERT INTO records (level, model_type, id, parent_id, fields_json) "
            "VALUES (?, ?, ?, ?, ?)",
            self._pending,
        )
        self._pending.clear()

    def field_order(self) -> dict[tuple[int, str], dict[str, None]]:
        """The per-(level, type) column order observed so far (for header building)."""
        return self._field_order

    def persist(self) -> None:
        """Flush buffered rows to the spill DB and commit, without writing any CSV.

        Used by a parallel parse worker: it only fills its spill; the central merge writes
        the CSVs.
        """
        self._drain()
        self._conn.commit()

    def groups(self, out_dir: Path, base: str) -> list["GroupSpec"]:
        """The per-(level, type) write jobs: filename + full header, in stable order."""
        out_dir = Path(out_dir)
        specs: list[GroupSpec] = []
        for level, model_type in sorted(self._field_order):
            header = (
                id_columns(level)
                + list(self._field_order[(level, model_type)])
                + list(SOURCE_COLUMNS)
            )
            filename = f"{base}_{LEVEL_FILE_PREFIX[level]}{model_type}.csv"
            specs.append(GroupSpec(level, model_type, header, out_dir / filename))
        return specs

    def flush(
        self, out_dir: Path, base: str, source: SourceIndex, *, workers: int = 1
    ) -> dict[str, int]:
        """Write one CSV per (level, type); return {filename: row_count}.

        ``workers`` > 1 fans the per-type writes out across processes (each opens the
        committed spill DB read-only); the parse is already done, so this is pure I/O.
        """
        self._drain()
        self._conn.commit()
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        specs = self.groups(out_dir, base)

        if workers > 1 and len(specs) > 1:
            # Imported lazily so a serial run never pulls in the multiprocessing machinery.
            from ufdr_parser.parallel import parallel_write_groups

            return parallel_write_groups(self._spill_path(), specs, workers)

        written: dict[str, int] = {}
        for spec in specs:
            count = write_group(self._conn, source.resolve, spec)
            written[spec.path.name] = count
            log.info(
                f"Wrote {count} {spec.model_type} record(s) (level {spec.level}) to {spec.path}"
            )
        return written

    def _spill_path(self) -> str:
        """Filesystem path of the spill database backing this connection."""
        for _seq, name, filename in self._conn.execute("PRAGMA database_list"):
            if name == "main":
                return filename
        raise RuntimeError("spill connection has no on-disk main database")


@dataclass(frozen=True)
class GroupSpec:
    """One CSV write job: which (level, type) goes to which file with which header."""

    level: int
    model_type: str
    header: list[str]
    path: Path


def id_columns(level: int) -> list[str]:
    """The identity columns written ahead of a record's fields, by nesting level."""
    return [COL_UUID] if level == LEVEL_TOP else [COL_SUB_UUID, COL_MAIN_UUID]


def write_rows(
    csv_writer: "csv.DictWriter",
    records_conn: sqlite3.Connection,
    level: int,
    model_type: str,
    resolve: "Callable[[str | None], object | None]",
) -> int:
    """Append one group's rows (no header) from ``records``, joining each to its source.

    Shared by the serial flush and the parallel merge so the row format has one definition.
    Rows are emitted in ``seq`` order (document order within a connection).
    """
    cursor = records_conn.execute(
        "SELECT id, parent_id, fields_json FROM records "
        "WHERE level = ? AND model_type = ? ORDER BY seq",
        (level, model_type),
    )
    count = 0
    for model_id, parent_id, fields_json in cursor:
        row: dict[str, str | None] = {}
        if level == LEVEL_TOP:
            row[COL_UUID] = model_id
        else:
            row[COL_SUB_UUID] = model_id
            row[COL_MAIN_UUID] = parent_id
        row.update(json.loads(fields_json))
        entry = resolve(model_id)
        if entry is not None:
            row.update(entry.as_columns())
        csv_writer.writerow(row)
        count += 1
    return count


def open_group_writer(path: Path, header: list[str]) -> "tuple[object, csv.DictWriter]":
    """Open a CSV at ``path`` and write its header; return (file handle, DictWriter).

    utf-8-sig prepends a BOM so Excel/Notepad auto-detect UTF-8 (accented names).
    """
    fh = path.open("w", encoding="utf-8-sig", newline="")
    csv_writer = csv.DictWriter(fh, fieldnames=header, restval="", extrasaction="ignore")
    csv_writer.writeheader()
    return fh, csv_writer


def write_group(
    records_conn: sqlite3.Connection,
    resolve: "Callable[[str | None], object | None]",
    spec: "GroupSpec",
) -> int:
    """Write one (level, type) CSV from a single ``records`` connection."""
    fh, csv_writer = open_group_writer(spec.path, spec.header)
    try:
        return write_rows(csv_writer, records_conn, spec.level, spec.model_type, resolve)
    finally:
        fh.close()
