"""Resolve a model id to its on-device source file via the ``<extraInfos>`` block.

Defines:    SourceEntry (one resolved source), parse_extra_info (element -> entry), and
            SourceIndex (a disk-backed id -> source map over a shared SQLite connection).
Used by:    dump (builds the index during the single pass, joins it at flush) and tests.
Depends on: const (tags), sqlite3, lxml element API.

WHY a disk-backed map: ``<extraInfos>`` sits at the END of report.xml and can hold
millions of entries (~421k in the 1.35 GB fixture). Holding the id->source map in RAM
would not scale to a 32 GB report, so it is staged in a temporary SQLite table keyed by
model id. The model's ``id`` (uuid) is the lookup key; the first ``<nodeInfo>`` carries
the full source path plus, for DB-derived records, the table name and byte offset.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from lxml import etree

from ufdr_parser.const import (
    COL_SOURCE_NAME,
    COL_SOURCE_OFFSET,
    COL_SOURCE_PATH,
    COL_SOURCE_SIZE,
    COL_SOURCE_TABLE,
    TAG_EXTRA_INFO,
    TAG_NODE_INFO,
)

_EXTRA_INFO_MODEL_TYPE = "model"
_BATCH = 5000


@dataclass(frozen=True)
class SourceEntry:
    """The source file a decoded model was recovered from."""

    model_id: str
    path: str | None
    name: str | None
    table: str | None
    offset: str | None
    size: str | None

    def as_columns(self) -> dict[str, str | None]:
        """Map to the CSV ``source_*`` columns."""
        return {
            COL_SOURCE_PATH: self.path,
            COL_SOURCE_NAME: self.name,
            COL_SOURCE_TABLE: self.table,
            COL_SOURCE_OFFSET: self.offset,
            COL_SOURCE_SIZE: self.size,
        }


def parse_extra_info(elem: "etree._Element") -> SourceEntry | None:
    """Build a SourceEntry from an ``<extraInfo type="model" id="..">`` element.

    Returns None for non-model extraInfo or when no node id is present. The first
    ``<nodeInfo>`` descendant supplies the source attributes.
    """
    if elem.get("type") != _EXTRA_INFO_MODEL_TYPE:
        return None
    model_id = elem.get("id")
    if not model_id:
        return None
    node = elem.find(f".//{TAG_NODE_INFO}")
    if node is None:
        return SourceEntry(model_id, None, None, None, None, None)
    return SourceEntry(
        model_id=model_id,
        path=node.get("path"),
        name=node.get("name"),
        table=node.get("tableName"),
        offset=node.get("offset"),
        size=node.get("size"),
    )


def index_source_stream(stream, conn: sqlite3.Connection) -> int:
    """Build the ``model_source`` map from a stream wrapping the ``<extraInfos>`` region.

    Streams extraInfo end events (freeing each), so it is flat-RAM for the trailing block of
    a 32 GB report. Returns the number of source entries indexed.
    """
    index = SourceIndex(conn)
    context = etree.iterparse(stream, events=("end",), tag=TAG_EXTRA_INFO, recover=True, huge_tree=True)
    count = 0
    for _event, elem in context:
        entry = parse_extra_info(elem)
        if entry is not None:
            index.add(entry)
            count += 1
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    index.finalise()
    return count


class SourceIndex:
    """A disk-backed model-id -> source map over a caller-owned SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._pending: list[tuple[str, str | None, str | None, str | None, str | None, str | None]] = []
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_source ("
            "id TEXT PRIMARY KEY, path TEXT, name TEXT, "
            "table_name TEXT, offset TEXT, size TEXT)"
        )

    def add(self, entry: SourceEntry) -> None:
        self._pending.append(
            (entry.model_id, entry.path, entry.name, entry.table, entry.offset, entry.size)
        )
        if len(self._pending) >= _BATCH:
            self._drain()

    def _drain(self) -> None:
        if not self._pending:
            return
        # INSERT OR IGNORE: the first nodeInfo for an id wins; later duplicates are noise.
        self._conn.executemany(
            "INSERT OR IGNORE INTO model_source VALUES (?, ?, ?, ?, ?, ?)", self._pending
        )
        self._pending.clear()

    def finalise(self) -> None:
        """Flush pending rows so resolve() sees every entry. Call before any lookups."""
        self._drain()
        self._conn.commit()

    def resolve(self, model_id: str | None) -> SourceEntry | None:
        return resolve_from_conn(self._conn, model_id)


def resolve_from_conn(conn: sqlite3.Connection, model_id: str | None) -> SourceEntry | None:
    """Look one model id up in a ``model_source`` table on any connection.

    Standalone so a read-only flush worker can resolve sources from its own connection
    to the spill DB without constructing a SourceIndex (which would try to create tables).
    """
    if not model_id:
        return None
    row = conn.execute(
        "SELECT id, path, name, table_name, offset, size "
        "FROM model_source WHERE id = ?",
        (model_id,),
    ).fetchone()
    return SourceEntry(*row) if row is not None else None
