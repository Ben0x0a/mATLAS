"""Resolve a model id to its on-device source file via the ``<extraInfos>`` block.

Defines:    SourceEntry (one resolved source), parse_extra_info (element -> entry), and
            SourceIndex (a disk-backed id -> source map over a shared SQLite connection).
Used by:    dump / parallel (build the index, join it at flush), the model_atlas Cellebrite
            XML dialect (cellebrite_xml_reports), and tests.
Depends on: const (tags), sqlite3, lxml element API.

WHY a disk-backed map: ``<extraInfos>`` sits at the END of report.xml and can hold
millions of entries (~860k in a 2.7 GB report). Holding the id->source map in RAM
would not scale to a 32 GB report, so it is staged in temporary SQLite tables keyed by
id. The model's ``id`` (uuid) is the lookup key; its first ``<nodeInfo>`` carries the
table name and byte offset for DB-derived records, and the source file itself in one of
two shapes:

- INLINE: ``<nodeInfo name=.. path=.. size=.. tableName=.. offset=..>`` (older reports).
- BY REFERENCE: ``<nodeInfo id=<file node id> tableName=.. offset=..>`` — the path, name and
  size live in a separate ``<extraInfo type="node" id=<file node id>>`` entry. Recent
  Physical Analyzer reports (reportVersion 8.5) use this for almost every model, so reading
  only the inline attributes left ``source_path`` blank on every row. A node can itself
  reference another node (e.g. a derived copy of a database), so the reference is followed
  until a node carrying a path is reached (bounded by ``_MAX_NODE_HOPS``).

Model and node entries are kept in separate tables (``model_source`` / ``node_source``) and the
references are resolved ONCE in ``SourceIndex.finalise`` (after the whole block is read, since
a reference may point either way in the document), so ``resolve_from_conn`` stays a plain
primary-key lookup for the serial flush, the parallel workers and the Cellebrite dialect.
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

# ``<extraInfo type=..>`` kinds: a decoded model's source, or a file node's own location.
EXTRA_INFO_MODEL_KIND = "model"
EXTRA_INFO_NODE_KIND = "node"
_BATCH = 5000
# Upper bound on node -> node reference hops followed in finalise. Real reports chain at most
# two (model -> derived node -> file node); the bound only stops a malformed cyclic chain.
_MAX_NODE_HOPS = 8


@dataclass(frozen=True)
class SourceEntry:
    """The source file a decoded model (or a file node) was recovered from.

    ``node_ref`` is the file node id of a BY-REFERENCE ``<nodeInfo id=..>`` (None when the
    location is inline); ``kind`` is the ``<extraInfo type>`` it came from.
    """

    model_id: str
    path: str | None
    name: str | None
    table: str | None
    offset: str | None
    size: str | None
    node_ref: str | None = None
    kind: str = EXTRA_INFO_MODEL_KIND

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
    """Build a SourceEntry from an ``<extraInfo type="model"|"node" id="..">`` element.

    Returns None for any other extraInfo kind, when no id is present, or for a node without a
    ``<nodeInfo>`` (nothing to resolve a reference to). The first ``<nodeInfo>`` descendant
    supplies the source attributes; its ``id`` attribute, when present, is the file node the
    location is held by (see the module docstring).
    """
    kind = elem.get("type")
    if kind not in (EXTRA_INFO_MODEL_KIND, EXTRA_INFO_NODE_KIND):
        return None
    entry_id = elem.get("id")
    if not entry_id:
        return None
    node = elem.find(f".//{TAG_NODE_INFO}")
    if node is None:
        if kind == EXTRA_INFO_NODE_KIND:
            return None
        return SourceEntry(entry_id, None, None, None, None, None)
    return SourceEntry(
        model_id=entry_id,
        path=node.get("path"),
        name=node.get("name"),
        table=node.get("tableName"),
        offset=node.get("offset"),
        size=node.get("size"),
        node_ref=node.get("id"),
        kind=kind,
    )


def index_source_stream(stream, conn: sqlite3.Connection) -> int:
    """Build the ``model_source`` map from a stream wrapping the ``<extraInfos>`` region.

    Streams extraInfo end events (freeing each), so it is flat-RAM for the trailing block of
    a 32 GB report. Returns the number of MODEL source entries indexed (file node entries
    only serve to resolve references, so they are not counted).
    """
    index = SourceIndex(conn)
    context = etree.iterparse(stream, events=("end",), tag=TAG_EXTRA_INFO, recover=True, huge_tree=True)
    count = 0
    for _event, elem in context:
        entry = parse_extra_info(elem)
        if entry is not None:
            index.add(entry)
            if entry.kind == EXTRA_INFO_MODEL_KIND:
                count += 1
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    index.finalise()
    return count


# One staging table per extraInfo kind; the names are fixed identifiers, never user input.
_TABLE_BY_KIND = {EXTRA_INFO_MODEL_KIND: "model_source", EXTRA_INFO_NODE_KIND: "node_source"}


class SourceIndex:
    """A disk-backed model-id -> source map over a caller-owned SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # kind -> pending rows (id, path, name, table_name, offset, size, node_ref).
        self._pending: dict[str, list[tuple[str | None, ...]]] = {
            EXTRA_INFO_MODEL_KIND: [],
            EXTRA_INFO_NODE_KIND: [],
        }
        for table in _TABLE_BY_KIND.values():
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "id TEXT PRIMARY KEY, path TEXT, name TEXT, "
                "table_name TEXT, offset TEXT, size TEXT, node_ref TEXT)"
            )

    def add(self, entry: SourceEntry) -> None:
        pending = self._pending[entry.kind]
        pending.append(
            (
                entry.model_id,
                entry.path,
                entry.name,
                entry.table,
                entry.offset,
                entry.size,
                entry.node_ref,
            )
        )
        if len(pending) >= _BATCH:
            self._drain()

    def _drain(self) -> None:
        for kind, pending in self._pending.items():
            if not pending:
                continue
            # INSERT OR IGNORE: the first nodeInfo for an id wins; later duplicates are noise.
            self._conn.executemany(
                f"INSERT OR IGNORE INTO {_TABLE_BY_KIND[kind]} VALUES (?, ?, ?, ?, ?, ?, ?)",
                pending,
            )
            pending.clear()

    def finalise(self) -> None:
        """Flush pending rows and resolve node references. Call before any lookups."""
        self._drain()
        self._resolve_node_refs()
        self._conn.commit()

    def _resolve_node_refs(self) -> None:
        """Copy path/name/size from the referenced file node onto each by-reference model.

        HOW: each pass joins every still-unresolved model (no path, a ``node_ref``) to the node
        it references, copies that node's path/name/size and advances ``node_ref`` to the
        node's own reference; a model whose node carried a path is then resolved (its
        ``node_ref`` becomes None). Passes repeat until none changes a row, at most
        ``_MAX_NODE_HOPS`` times. The model keeps its OWN table_name/offset: those locate the
        record inside the file, whereas the node's are always blank/0.
        WHY bounded: an id that references itself (or a longer cycle) would otherwise loop for
        ever; after the bound such a model simply keeps a blank path, as before this fix.
        """
        for _hop in range(_MAX_NODE_HOPS):
            cursor = self._conn.execute(
                "UPDATE model_source SET path = n.path, name = n.name, size = n.size, "
                "node_ref = n.node_ref "
                "FROM node_source AS n "
                "WHERE model_source.path IS NULL AND model_source.node_ref = n.id"
            )
            if cursor.rowcount == 0:
                return

    def resolve(self, model_id: str | None) -> SourceEntry | None:
        return resolve_from_conn(self._conn, model_id)


def resolve_from_conn(conn: sqlite3.Connection, model_id: str | None) -> SourceEntry | None:
    """Look one model id up in a ``model_source`` table on any connection.

    Standalone so a read-only flush worker can resolve sources from its own connection
    to the spill DB without constructing a SourceIndex (which would try to create tables).
    Node references are already resolved by ``SourceIndex.finalise``.
    """
    if not model_id:
        return None
    row = conn.execute(
        "SELECT id, path, name, table_name, offset, size, node_ref "
        "FROM model_source WHERE id = ?",
        (model_id,),
    ).fetchone()
    return SourceEntry(*row) if row is not None else None
