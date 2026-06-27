"""Cellebrite UFED report (``report/2.0``) XML dialect: model type -> pandas DataFrame.

Defines:    CellebriteDialect (registered under the Cellebrite namespace) and its prepare /
            model_types / read_model, plus the inline flatten helper.
Used by:    xml_reader's dialect registry (imported for its registration side effect via
            xml_specifics/__init__).
Depends on: xml_reader (shared SOURCE_COLUMNS / OpenStream / _local), ufdr_parser
            (scan / BlockRangeReader / source_lookup — one-way reuse), lxml, pandas.

WHY reuse ufdr_parser: its streaming scan, byte-range block reader, and extraInfos source
map are parity-verified; this module imports them as a library (ufdr_parser never depends
back on model_atlas). The flattening differs from the dumper, though: the integration model
wants a model's 1:1 nested values (a Coordinate's lat/lon, a StreetAddress's city) on the
SAME row, so 1:1 ``modelField`` children are inlined as ``Parent.Child`` columns here;
``multiModelField`` (1:many, e.g. a Call's Parties) is deferred to the transform layer.

The report is read IN PLACE via an OpenStream factory (a loose .xml file or a zip entry),
never copied — each pass opens a fresh seekable stream and seeks to the bytes it needs.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from lxml import etree

from model_atlas.sources.readers.xml_reader import (
    SOURCE_COLUMNS,
    OpenStream,
    _local,
    register_dialect,
)
from ufdr_parser.const import LOCAL_MODEL_TYPE, NS, TAG_MODEL, TAG_VALUE
from ufdr_parser.reader import BlockRangeReader, extra_infos_stream
from ufdr_parser.scan import DecodedIndex, index_model_types
from ufdr_parser.source_lookup import SourceIndex, index_source_stream, resolve_from_conn

log = logging.getLogger(__name__)

_LOCAL_FIELD = "field"
_LOCAL_MODEL_FIELD = "modelField"
# _LOCAL_MULTI_MODEL_FIELD ("multiModelField", 1:many) is intentionally not inlined.


@dataclass
class CellebritePrep:
    """Once-per-report state reused across model types: the block index + the source map."""

    open_stream: OpenStream
    index: DecodedIndex
    source_db: Path | None


def _flatten_inline(model_elem: "etree._Element", prefix: str = "") -> dict[str, str | None]:
    """Flatten a model's own fields plus its 1:1 modelField children (recursively).

    ``<field name=X>`` -> ``<prefix>X``; ``<modelField name=Position>`` -> its single child
    model flattened under ``Position.``. multiModelField (1:many) is skipped. For the
    TOP-LEVEL model the forensic attributes are also captured as columns: ``uuid`` (the
    stable Cellebrite id -> source_record_uid), ``deleted_state`` (-> deleted), and
    ``decoding_confidence``.
    """
    row: dict[str, str | None] = {}
    if not prefix:
        row["uuid"] = model_elem.get("id")
        row["deleted_state"] = model_elem.get("deleted_state")
        row["decoding_confidence"] = model_elem.get("decoding_confidence")
    for child in model_elem:
        local = _local(child.tag)
        if local == _LOCAL_FIELD:
            name = child.get("name")
            if name is not None:
                value = child.find(TAG_VALUE)
                row[f"{prefix}{name}"] = value.text if value is not None else None
                # For timestamps, also keep Cellebrite's zone-known flag (TimeStampKnown vs
                # DateTimeOnly) so the analyst sees whether the offset was real or assumed.
                if value is not None and value.get("type") == "TimeStamp":
                    row[f"{prefix}{name}.format"] = value.get("format")
        elif local == _LOCAL_MODEL_FIELD:
            nested = child.find(TAG_MODEL)
            if nested is not None:
                key = child.get("name") or nested.get("type") or "nested"
                row.update(_flatten_inline(nested, prefix=f"{prefix}{key}."))
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

        source_conn = (
            sqlite3.connect(f"file:{prep.source_db}?mode=ro", uri=True)
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
