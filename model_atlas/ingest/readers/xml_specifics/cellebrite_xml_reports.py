"""Cellebrite UFED report (``report/2.0``) XML dialect: model type -> pandas DataFrame.

Defines:    CellebriteDialect (registered under the Cellebrite namespace) and its prepare /
            model_types / read_model, plus the inline flatten helper.
Used by:    xml_reader's dialect registry (imported for its registration side effect via
            xml_specifics/__init__).
Depends on: xml_reader (shared SOURCE_COLUMNS / OpenStream / _local), ufdr_parser
            (scan / BlockRangeReader / source_lookup — one-way reuse), lxml, pandas.

Ported verbatim from ``sources/readers/xml_specifics/cellebrite_xml_reports.py``
(only the import of the shared helpers now points at ``model_atlas.ingest.readers.
xml_reader`` instead of ``sources.readers.xml_reader``) — the flattening and
block-scan logic are unchanged.

WHY reuse ufdr_parser: its streaming scan, byte-range block reader, and extraInfos source
map are parity-verified; this module imports them as a library (ufdr_parser never depends
back on model_atlas). The flattening differs from the dumper, though: the integration model
wants a model's nested values (a Coordinate's lat/lon, a Call's Parties) on the SAME row as
the parent, so 1:1 ``modelField`` children are inlined as ``Parent.Child`` columns and 1:many
``multiModelField`` children as indexed ``Parent.N.Child`` columns. WHY inline the 1:many
here rather than defer: NOTHING in the transform pipeline re-reads the report — this reader
is the sole path from a .ufdr into the pipeline (the standalone ufdr_parser dumper, which
emits each nested model as its own CSV row, is a separate GUI feature that never feeds the
pipeline). So if the multiModelField children were skipped, that nested evidence (e.g. a
Call's Parties) would be silently and unrecoverably dropped from integration.

The report is read IN PLACE via an OpenStream factory (a loose .xml file), never copied —
each pass opens a fresh seekable stream and seeks to the bytes it needs.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from lxml import etree

from model_atlas.ingest.readers.xml_reader import (
    SOURCE_COLUMNS,
    OpenStream,
    _local,
    register_dialect,
)
from ufdr_parser.const import LOCAL_MODEL, LOCAL_MODEL_TYPE, NS, TAG_MODEL, TAG_VALUE
from ufdr_parser.reader import BlockRangeReader, extra_infos_stream
from ufdr_parser.scan import DecodedIndex, index_model_types
from ufdr_parser.source_lookup import SourceIndex, index_source_stream, resolve_from_conn

log = logging.getLogger(__name__)

_LOCAL_FIELD = "field"
_LOCAL_MODEL_FIELD = "modelField"
_LOCAL_MULTI_MODEL_FIELD = "multiModelField"  # 1:many; inlined as indexed Parent.N.Child

# Forensic attributes injected onto every TOP-LEVEL row. These names are RESERVED: a model
# <field> that happens to share one is disambiguated (see _put) rather than overwriting the
# captured attribute, and the cellebrite_* presets map these column names verbatim
# (source_record_uid: field(uuid); deleted: field(deleted_state)).
_ATTR_UUID_COLNAME = "uuid"
_ATTR_DELETED_STATE_COLNAME = "deleted_state"
_ATTR_DECODING_CONFIDENCE_COLNAME = "decoding_confidence"


def _put(row: dict[str, str | None], key: str, value: str | None) -> str:
    """Insert ``key`` -> ``value``, disambiguating a duplicate with a ``#n`` suffix.

    HOW: if ``key`` is free it is used as-is; otherwise the first free ``key#2``/``key#3``/...
    is used. WHY: a model can legitimately carry two ``<field name="X">`` and a field can be
    named like an injected forensic attribute (``uuid``); keying only by name would let the
    later value silently OVERWRITE the earlier one (or the captured attribute), breaking the
    lossless, verbatim capture this auditable reader exists to guarantee. Returns the actual
    key used, so a derived sub-key (a TimeStamp ``.format``) attaches to the same column.
    """
    if key not in row:
        row[key] = value
        return key
    index = 2
    while f"{key}#{index}" in row:
        index += 1
    disambiguated = f"{key}#{index}"
    row[disambiguated] = value
    return disambiguated


@dataclass
class CellebritePrep:
    """Once-per-report state reused across model types: the block index + the source map."""

    open_stream: OpenStream
    index: DecodedIndex
    source_db: Path | None


def _flatten_inline(
    model_elem: "etree._Element",
    prefix: str = "",
    row: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    """Flatten a model's own fields plus its nested child models (recursively).

    ``<field name=X>`` -> ``<prefix>X``; a 1:1 ``<modelField name=Position>`` -> its single
    child flattened under ``Position.``; a 1:many ``<multiModelField name=Parties>`` -> each
    of its children flattened under ``Parties.0.`` / ``Parties.1.`` / ... so every sub-model
    (e.g. a Call's Parties) surfaces on the parent row and NONE is dropped. For the TOP-LEVEL
    model the forensic attributes are captured first as reserved columns: ``uuid`` (the stable
    Cellebrite id -> source_record_uid), ``deleted_state`` (-> deleted), and
    ``decoding_confidence``. All writes go through ``_put`` so a repeated or attribute-shadowing
    field name is disambiguated rather than clobbering a prior value.

    ``row`` is the accumulator threaded through the recursion (a fresh dict at the top level),
    so nested flattening cannot silently overwrite the parent's columns.
    """
    if row is None:
        row = {}
    if not prefix:
        # Injected FIRST so their names are taken; a later <field name="uuid"> lands on
        # "uuid#2" via _put and the captured attribute survives verbatim.
        row[_ATTR_UUID_COLNAME] = model_elem.get("id")
        row[_ATTR_DELETED_STATE_COLNAME] = model_elem.get("deleted_state")
        row[_ATTR_DECODING_CONFIDENCE_COLNAME] = model_elem.get("decoding_confidence")
    for child in model_elem:
        local = _local(child.tag)
        if local == _LOCAL_FIELD:
            name = child.get("name")
            if name is not None:
                value = child.find(TAG_VALUE)
                actual = _put(row, f"{prefix}{name}", value.text if value is not None else None)
                # For timestamps, also keep Cellebrite's zone-known flag (TimeStampKnown vs
                # DateTimeOnly) so the analyst sees whether the offset was real or assumed.
                if value is not None and value.get("type") == "TimeStamp":
                    _put(row, f"{actual}.format", value.get("format"))
        elif local == _LOCAL_MODEL_FIELD:
            nested = child.find(TAG_MODEL)
            if nested is not None:
                key = child.get("name") or nested.get("type") or "nested"
                _flatten_inline(nested, prefix=f"{prefix}{key}.", row=row)
        elif local == _LOCAL_MULTI_MODEL_FIELD:
            key = child.get("name") or "nested"
            # Index each sub-model so 1:many children never collide with one another; the
            # index preserves document order (Party 0, Party 1, ...) on the parent row.
            children = (sub for sub in child if _local(sub.tag) == LOCAL_MODEL)
            for position, sub in enumerate(children):
                _flatten_inline(sub, prefix=f"{prefix}{key}.{position}.", row=row)
    return row


def _iter_block(open_stream: OpenStream, start: int, end: int):
    """Yield (model_id, inline-flattened row) for each top-level model in a block range."""
    stream = open_stream()
    try:
        stream.seek(start)
        reader = BlockRangeReader(stream, end - start)
        context = etree.iterparse(reader, events=("end",), recover=True, huge_tree=True)
        for _event, elem in context:
            if elem.tag != TAG_MODEL:
                continue
            parent = elem.getparent()
            if parent is not None and _local(parent.tag) == LOCAL_MODEL_TYPE:
                yield elem.get("id"), _flatten_inline(elem)
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
    finally:
        stream.close()


class CellebriteDialect:
    """The ``report/2.0`` dialect: seek-parse one modelType block into a DataFrame."""

    namespace = NS

    def prepare(self, open_stream: OpenStream, work_dir: Path) -> CellebritePrep:
        with open_stream() as stream:
            index = index_model_types(stream)
        source_db = self._build_source_db(open_stream, index, Path(work_dir))
        return CellebritePrep(open_stream=open_stream, index=index, source_db=source_db)

    def model_types(self, prep: CellebritePrep) -> list[str]:
        return [block.model_type for block in prep.index.blocks]

    def read_model(self, prep: CellebritePrep, model_type: str) -> tuple[pd.DataFrame, tuple[str, ...]]:
        blocks = [b for b in prep.index.blocks if b.model_type == model_type]
        if not blocks:
            return pd.DataFrame(), ()

        # Build the read-only URI via Path.as_uri(), which percent-encodes the path. WHY:
        # the work-dir path is not ours to constrain and may contain '#', '?' or '%'; splicing
        # it raw into "file:{path}?mode=ro" lets SQLite's URI parser mis-read those as the
        # fragment/query/escape delimiters, raising OperationalError and aborting a model read
        # whose source map was built correctly. as_uri() yields a fully-encoded file:/// URI.
        source_conn = (
            sqlite3.connect(f"{prep.source_db.as_uri()}?mode=ro", uri=True)
            if prep.source_db
            else None
        )
        rows: list[dict[str, str | None]] = []
        field_order: dict[str, None] = {}  # first-seen field order across rows
        try:
            for block in blocks:
                for model_id, row in _iter_block(prep.open_stream, block.start, block.end):
                    for key in row:
                        field_order.setdefault(key, None)
                    if source_conn is not None:
                        entry = resolve_from_conn(source_conn, model_id)
                        if entry is not None:
                            row["source_path"] = entry.path
                            row["source_name"] = entry.name
                            row["source_table"] = entry.table
                            row["source_offset"] = entry.offset
                    rows.append(row)
        finally:
            if source_conn is not None:
                source_conn.close()

        # Source columns are part of the inventory so a preset can map them (e.g.
        # raw_source_path: column(source_path)) and they pass through as orig_* columns.
        columns = list(field_order) + list(SOURCE_COLUMNS)
        return pd.DataFrame(rows, columns=columns), tuple(columns)

    @staticmethod
    def _build_source_db(open_stream: OpenStream, index: DecodedIndex, work_dir: Path) -> Path:
        """Build the model-id -> source map (from extraInfos) once; reused for all types.

        Only this small derived index is written to disk — never a copy of the report.
        """
        db_path = work_dir / "xml_source.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        try:
            if index.extra_info_start is not None:
                with open_stream() as stream:
                    stream.seek(index.extra_info_start)
                    index_source_stream(extra_infos_stream(stream), conn)
            else:
                SourceIndex(conn).finalise()  # empty map
        finally:
            conn.close()
        return db_path


register_dialect(CellebriteDialect())
