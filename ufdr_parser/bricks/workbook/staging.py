"""Stage a dump's per-(level, type) CSVs into a temporary SQLite database for joining.

Defines:    Staging (lazy CSV -> table loader over a caller-owned connection) and Table (one
            staged group: its SQL name and its column order).
Used by:    workbook.links and workbook.sheets (every combined sheet reads through it).
Depends on: standard library csv / sqlite3.

WHY SQLite rather than pandas: a 32 GB report yields CSVs far larger than RAM, and the
combined sheets are joins (message -> its parties -> its chat's participants). Staging each
CSV once into an indexed table keeps every join an index lookup and the run flat in memory,
the same reason the dumper spills to SQLite. Every value stays TEXT exactly as written in the
CSV (the workbook is a re-arrangement of the raw output, never a re-interpretation of it).
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ufdr_parser.const import COL_MAIN_UUID, COL_SUB_UUID, COL_UUID

# csv.field_size_limit must fit a C long on every platform (Windows: 32-bit), so not maxsize.
# A cell larger than this would also be far past what any spreadsheet can display.
_CSV_FIELD_LIMIT = 2**31 - 1
_BATCH = 5000


def quote(name: str) -> str:
    """Quote an SQL identifier (column names come from report field names, e.g. ``a"b``)."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Table:
    """One staged (level, model_type) group."""

    name: str
    columns: tuple[str, ...]

    def has(self, column: str) -> bool:
        return column in self.columns

    def col(self, column: str, alias: str | None = None) -> str:
        """SQL expression for ``column``, or NULL when this dump has no such column.

        WHY: a model type's columns are whatever fields the report used, so a sheet that names
        e.g. ``Account`` must still work on a report whose Calls never carry it.
        """
        prefix = f"{alias}." if alias else ""
        return f"{prefix}{quote(column)}" if column in self.columns else "NULL"


class Staging:
    """Lazily loads the dump CSVs a sheet asks for into ``conn`` (one table per group)."""

    def __init__(self, conn: sqlite3.Connection, group_paths: dict[tuple[int, str], Path]) -> None:
        self._conn = conn
        self._paths = group_paths
        self._tables: dict[tuple[int, str], Table | None] = {}

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def groups(self) -> list[tuple[int, str]]:
        """Every (level, model_type) group the dump wrote, sorted."""
        return sorted(self._paths)

    def csv_name(self, level: int, model_type: str) -> str | None:
        path = self._paths.get((level, model_type))
        return path.name if path is not None else None

    def table(self, level: int, model_type: str) -> Table | None:
        """The staged table for a group, loading its CSV on first use; None if not dumped."""
        key = (level, model_type)
        if key not in self._tables:
            path = self._paths.get(key)
            self._tables[key] = self._load(key, path) if path is not None else None
        return self._tables[key]

    def _load(self, key: tuple[int, str], path: Path) -> Table:
        level, model_type = key
        name = quote(f"g{level}_{model_type}")
        csv.field_size_limit(_CSV_FIELD_LIMIT)
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            # seq preserves CSV (= document) order, the order every sheet falls back on.
            column_sql = ", ".join(f"{quote(column)} TEXT" for column in header)
            self._conn.execute(
                f"CREATE TABLE {name} (seq INTEGER PRIMARY KEY, {column_sql})"
            )
            insert = (
                f"INSERT INTO {name} ({', '.join(quote(c) for c in header)}) "
                f"VALUES ({', '.join('?' for _ in header)})"
            )
            batch: list[list[str | None]] = []
            for row in reader:
                # Blank cells come back as NULL so COALESCE / IS NULL behave as expected.
                batch.append([value if value != "" else None for value in row])
                if len(batch) >= _BATCH:
                    self._conn.executemany(insert, batch)
                    batch.clear()
            if batch:
                self._conn.executemany(insert, batch)
        for column in (COL_UUID, COL_SUB_UUID, COL_MAIN_UUID):
            if column in header:
                self._conn.execute(
                    f"CREATE INDEX {quote(f'ix_g{level}_{model_type}_{column}')} "
                    f"ON {name}({quote(column)})"
                )
        return Table(name=name, columns=tuple(header))
