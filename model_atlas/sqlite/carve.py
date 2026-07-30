"""Carve deleted rows from a SQLite file's unallocated space (freelist + free space).

Defines:    ``carve_table`` — scan a ``.sqlite`` file's raw b-tree bytes for records that
            match a target table's schema but no longer live in the tree, plus the
            provenance vocabulary (``CARVE_SOURCE_COLNAME`` /
            ``FREELIST_SOURCE_NAME`` / ``FREESPACE_SOURCE_NAME``) that tags each carved
            row by WHERE it was recovered from.
Used by:    ``model_atlas.ingest.readers.sqlite_reader`` (an ADDITIONAL recovery pass
            after the WAL/journal two-pass merge — its carved rows join the reader's
            ``recovered`` list and are numbered AFTER live rows by the shared
            ``core.determinism.numbered_records`` rule).
Depends on: ``model_atlas.sqlite._sql.quote_identifier`` (the one strict identifier
            path, reused for the schema PRAGMA) and the standard library only
            (``mmap``, ``sqlite3`` for read-only schema introspection, ``struct``).

WHY this brick exists: mATLAS already recovers deleted rows from WAL/journal sidecars,
but once a WAL has been checkpointed away those sidecars are gone — the deleted content
survives ONLY inside the main ``.sqlite`` file, in two places SQLite leaves untouched
until the space is reused:

  * **freelist pages** — whole pages SQLite has released for reuse (e.g. a dropped
    table). Their prior cell/row bytes persist until the page is reallocated.
  * **free space inside a leaf-table b-tree page** — the unallocated gap between the
    cell-pointer array and the live cell content, plus the freeblock chain a DELETE
    leaves behind. A deleted row's cell bytes usually remain until that space is reused.

WHY raw bytes, not SQLite's API: SQLite's API returns only LIVE rows and would replay a
journal, so deleted content is unreachable through it. The carver therefore reads the
file's raw page bytes directly and reconstructs records by hand. Schema (column count +
type affinities) is the ONLY thing read through SQLite, via a read-only ``immutable=1``
connection — the same read-only introspection the reader already does in ``subunits`` /
``peek_fields``; it never mutates the evidence.

Carving is ON BY DEFAULT (see the reader): "recovery that silently drops data" is the
failure mode this brick exists to avoid, so a table read carves unless a caller opts out.

Precision over recall: a carved buffer is scanned byte-by-byte for a plausible record
header, so FALSE POSITIVES are expected (random bytes that happen to decode as a
schema-shaped record). Every carved row is therefore marked RECOVERED by the reader and
tagged with its freelist/freespace provenance here — never mixed in with live evidence.

Determinism: the carve is a pure function of the file bytes. Pages are visited in
ASCENDING page-number order; within a page, regions in a fixed order (unallocated gap
first, then the freeblock chain in chain order); within a region, offsets ascending. A
re-run over the same bytes therefore yields a byte-identical carved-row sequence, so the
``line_no`` the reader assigns each carved row is stable between runs.
"""
from __future__ import annotations

import contextlib
import logging
import mmap
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from model_atlas.sqlite._sql import quote_identifier

log = logging.getLogger(__name__)

# --- Provenance vocabulary (mirrors dedup.SOURCE_COLNAME for the sidecar path) ------

# The metadata key a carved row carries so the reader can tell freelist from free-space
# recovery. It is a private "_meta_" key (never a real source column) that the reader
# pops before numbering — exactly as it pops dedup's SOURCE_COLNAME for WAL/journal rows.
CARVE_SOURCE_COLNAME = "_meta_sqlite_carve_source"
FREELIST_SOURCE_NAME = "freelist"
FREESPACE_SOURCE_NAME = "freespace"

# --- SQLite on-disk format constants ------------------------------------------------

_HEADER_MAGIC = b"SQLite format 3\x00"
_LEAF_TABLE_BTREE = 0x0D
# Page 1 carries the 100-byte database header before its b-tree page header, so cells on
# page 1 begin after byte 100 even though its page header still starts at byte 100.
_DB_HEADER_SIZE = 100

# Type-affinity buckets we compute from a declared column type (SQLite §3.1).
_AFF_INTEGER = "INTEGER"
_AFF_REAL = "REAL"
_AFF_TEXT = "TEXT"
_AFF_BLOB = "BLOB"
_AFF_NUMERIC = "NUMERIC"


# --- varint -------------------------------------------------------------------------

def _read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    """Decode a SQLite big-endian variable-length integer at ``buf[offset:]``.

    HOW: a varint is 1-9 bytes. For the first 1-8 bytes the high bit (0x80) is a
    continuation flag and the low 7 bits contribute MSB-first; if 8 continuation bytes
    are consumed the 9th byte contributes ALL 8 bits (that ninth byte lets a varint span
    the full 64-bit range 8x7 bits cannot).
    Returns ``(value, bytes_consumed)``; raises ``ValueError`` if the buffer ends
    mid-varint (mapped by the scanner to an ordinary "no record here" outcome).
    """
    value = 0
    for i in range(8):
        if offset + i >= len(buf):
            raise ValueError("buffer too short for varint")
        byte = buf[offset + i]
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            return value, i + 1
    if offset + 8 >= len(buf):
        raise ValueError("buffer too short for varint")
    value = (value << 8) | buf[offset + 8]
    return value, 9


# --- file / page parsing ------------------------------------------------------------

@dataclass(frozen=True)
class _FileInfo:
    page_size: int
    first_freelist_trunk: int
    total_pages: int  # derived from the actual file length, not the header's claim


def _parse_file_header(data: bytes | mmap.mmap) -> _FileInfo:
    """Read the 100-byte database header. Raises ``ValueError`` on a non-SQLite file.

    WHY slice-compare rather than ``.startswith``: ``data`` may be an ``mmap`` view,
    which does not implement ``startswith``.
    """
    if data[: len(_HEADER_MAGIC)] != _HEADER_MAGIC:
        raise ValueError("not a SQLite database (bad magic)")
    page_size = struct.unpack(">H", data[16:18])[0]
    if page_size == 1:
        # §1.3: a stored page-size of 1 encodes 65536 (the value does not fit in u16).
        page_size = 65536
    if page_size == 0:
        # A valid SQLite page size is a power of two in 512..65536; 0 is impossible and
        # would ZeroDivision the ``len(data) // page_size`` below. Reject with a clean,
        # typed error (as the bad-magic guard above does) so ``carve_table`` — the public
        # entry — degrades to "no carved rows" instead of crashing on a crafted header.
        raise ValueError("not a SQLite database (page size 0)")
    first_freelist_trunk = struct.unpack(">I", data[32:36])[0]
    total_pages = len(data) // page_size
    return _FileInfo(
        page_size=page_size,
        first_freelist_trunk=first_freelist_trunk,
        total_pages=total_pages,
    )


def _get_page(data: bytes | mmap.mmap, page_no: int, page_size: int) -> bytes:
    """Return page ``page_no`` (1-indexed). Page 1 begins with the 100-byte db header."""
    start = (page_no - 1) * page_size
    return bytes(data[start : start + page_size])


def _collect_freelist_pages(data: bytes | mmap.mmap, info: _FileInfo) -> set[int]:
    """Walk the freelist trunk chain and return every freelist page number (trunks +
    leaves).

    HOW: the header points at the first trunk page; each trunk page is
    ``[u32 next-trunk][u32 leaf-count][leaf-count x u32 leaf-page-no]``. A ``visited``
    set breaks any corrupt cycle, and every page number is range-checked against the
    file length so a bogus pointer cannot index outside the mapping.
    """
    pages: set[int] = set()
    trunk = info.first_freelist_trunk
    visited: set[int] = set()
    while trunk and trunk not in visited and 1 <= trunk <= info.total_pages:
        visited.add(trunk)
        pages.add(trunk)
        page = _get_page(data, trunk, info.page_size)
        if len(page) < 8:
            break
        next_trunk = struct.unpack(">I", page[0:4])[0]
        n_leaves = struct.unpack(">I", page[4:8])[0]
        for i in range(n_leaves):
            off = 8 + i * 4
            if off + 4 > len(page):
                break
            leaf = struct.unpack(">I", page[off : off + 4])[0]
            if 1 <= leaf <= info.total_pages:
                pages.add(leaf)
        trunk = next_trunk
    return pages


def _leaf_table_freespace_regions(
    page: bytes, *, is_page_1: bool,
) -> list[tuple[bytes, int]]:
    """Return ``(region_bytes, offset_in_page)`` for each free-space area of a leaf-table
    b-tree page; ``[]`` for any other page type.

    Leaf b-tree page header (at page offset 0, or 100 on page 1):
        0: u8  page type (0x0D for a leaf table)
        1: u16 first freeblock offset (0 = none)
        3: u16 number of cells
        5: u16 cell-content-area start (0 encodes 65536)
        7: u8  fragmented free bytes
        8..    cell-pointer array (u16 each)
    """
    header_off = _DB_HEADER_SIZE if is_page_1 else 0
    if len(page) < header_off + 8 or page[header_off] != _LEAF_TABLE_BTREE:
        return []

    first_freeblock = struct.unpack(">H", page[header_off + 1 : header_off + 3])[0]
    num_cells = struct.unpack(">H", page[header_off + 3 : header_off + 5])[0]
    cell_area_start = struct.unpack(">H", page[header_off + 5 : header_off + 7])[0]
    if cell_area_start == 0:
        # §1.6: a stored 0 in this field means the cell content area starts at 65536
        # (a fully-packed page whose content begins exactly at the page boundary).
        cell_area_start = 65536

    regions: list[tuple[bytes, int]] = []

    # Region 1 (fixed order, first): the unallocated gap between the end of the
    # cell-pointer array and the start of the live cell content.
    cell_ptr_end = header_off + 8 + num_cells * 2
    if cell_area_start > cell_ptr_end:
        regions.append((page[cell_ptr_end:cell_area_start], cell_ptr_end))

    # Region 2 (fixed order, then): the freeblock chain a DELETE leaves behind. Each
    # freeblock is [u16 next][u16 size]; its payload runs to the next freeblock. A
    # ``seen`` set plus an explicit iteration cap stops a corrupt/looping chain from
    # starving the carve — the consequence of removing them is an unbounded scan on a
    # hostile input.
    seen: set[int] = set()
    freeblock = first_freeblock
    max_freeblocks = len(page) // 4  # 4 = minimum freeblock header size
    while freeblock and freeblock not in seen and freeblock + 4 <= len(page):
        if len(seen) >= max_freeblocks:
            break
        seen.add(freeblock)
        next_freeblock = struct.unpack(">H", page[freeblock : freeblock + 2])[0]
        size = struct.unpack(">H", page[freeblock + 2 : freeblock + 4])[0]
        end = min(freeblock + size, len(page))
        regions.append((page[freeblock + 4 : end], freeblock + 4))
        freeblock = next_freeblock
    return regions


# --- target-table schema ------------------------------------------------------------

@dataclass(frozen=True)
class _ColumnSchema:
    name: str
    affinity: str
    is_int_pk: bool  # an INTEGER PRIMARY KEY column (a rowid alias)


@dataclass(frozen=True)
class _TableSchema:
    columns: list[_ColumnSchema]

    @property
    def n_cols(self) -> int:
        return len(self.columns)


def _affinity(declared_type: str) -> str:
    """SQLite's type-affinity algorithm (§3.1) over a declared column type."""
    text = (declared_type or "").upper()
    if "INT" in text:
        return _AFF_INTEGER
    if "CHAR" in text or "CLOB" in text or "TEXT" in text:
        return _AFF_TEXT
    if "BLOB" in text or text == "":
        return _AFF_BLOB
    if "REAL" in text or "FLOA" in text or "DOUB" in text:
        return _AFF_REAL
    return _AFF_NUMERIC


def _read_table_schema(source: Path, table: str) -> _TableSchema:
    """Read ``table``'s column layout via a read-only ``immutable=1`` connection.

    WHY read-only immutable: schema is the only thing carving needs from SQLite, and
    ``mode=ro&immutable=1`` guarantees the evidence file is never written or journalled —
    the same guarantee the reader's ``subunits`` / ``peek_fields`` already rely on.
    """
    quoted = quote_identifier(table)
    uri = f"file:{source}?mode=ro&immutable=1"
    # contextlib.closing (not a bare ``with sqlite3.connect``): sqlite3's own context
    # manager commits/rolls back but does NOT close the connection, so carving many files
    # would leak a handle per call — the reader uses ``closing`` for the same reason.
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        # PRAGMA table_info row: (cid, name, type, notnull, dflt, pk).
        rows = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
    if not rows:
        raise ValueError(f"no such table: {table!r}")
    columns = [
        _ColumnSchema(
            name=name,
            affinity=_affinity(dtype or ""),
            is_int_pk=(pk == 1 and (dtype or "").upper() == "INTEGER"),
        )
        for _cid, name, dtype, _notnull, _dflt, pk in rows
    ]
    return _TableSchema(columns=columns)


# --- record / cell decoding ---------------------------------------------------------

def _serial_type_size(serial: int) -> int:
    """Byte length of a value with SQLite serial type ``serial`` (-1 = reserved 10/11)."""
    fixed = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}
    if serial in fixed:
        return fixed[serial]
    if serial >= 12 and serial % 2 == 0:  # BLOB of (serial-12)/2 bytes
        return (serial - 12) // 2
    if serial >= 13:  # TEXT of (serial-13)/2 bytes
        return (serial - 13) // 2
    return -1  # 10, 11 are reserved for internal use


def _serial_compatible_with_affinity(serial: int, affinity: str) -> bool:
    """WHY a plausibility gate, not exact typing: a carved header is a GUESS, so we reject
    serial types a column's affinity would never have produced. This trades recall for
    precision — the deliberate bias of this brick."""
    if serial == 0:  # a NULL is always plausible
        return True
    if affinity == _AFF_INTEGER:
        return serial in (1, 2, 3, 4, 5, 6, 8, 9)
    if affinity == _AFF_REAL:
        return serial in (1, 2, 3, 4, 5, 6, 7, 8, 9)
    if affinity == _AFF_TEXT:
        return serial >= 13 and serial % 2 == 1
    if affinity == _AFF_BLOB:
        return serial not in (10, 11)
    if affinity == _AFF_NUMERIC:
        return serial in (1, 2, 3, 4, 5, 6, 7, 8, 9) or (serial >= 13 and serial % 2 == 1)
    return False


def _decode_value(buf: bytes, offset: int, serial: int) -> tuple[Any, int]:
    """Decode one column value at ``buf[offset:]``. Returns ``(value, bytes_used)``.

    Values land in their native Python types (int / float / str / bytes / None) — the
    SAME types the reader's live ``SELECT`` yields — so a carved BLOB (bytes) flows
    through the reader's BLOB-envelope step identically to a live one.
    """
    if serial == 0:
        return None, 0
    if serial in (1, 2, 3, 4, 5, 6):
        widths = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8}
        n = widths[serial]
        raw = buf[offset : offset + n]
        if len(raw) != n:
            raise ValueError("truncated integer")
        # Big-endian, two's-complement; sign-extend the 3- and 6-byte widths to 8 bytes.
        padded = (b"\xff" if raw[0] & 0x80 else b"\x00") * (8 - n) + raw
        return struct.unpack(">q", padded)[0], n
    if serial == 7:
        raw = buf[offset : offset + 8]
        if len(raw) != 8:
            raise ValueError("truncated float")
        return struct.unpack(">d", raw)[0], 8
    if serial == 8:
        return 0, 0  # constant 0 (no bytes on disk)
    if serial == 9:
        return 1, 0  # constant 1 (no bytes on disk)
    size = _serial_type_size(serial)
    if size < 0:
        raise ValueError(f"reserved serial type {serial}")
    raw = buf[offset : offset + size]
    if len(raw) != size:
        raise ValueError("truncated blob/text")
    if serial >= 13 and serial % 2 == 1:  # TEXT
        try:
            return raw.decode("utf-8"), size
        except UnicodeDecodeError as exc:
            # WHY reject rather than replace: a cell that will not decode as UTF-8 is
            # almost certainly a mis-aligned false positive, not real text.
            raise ValueError("invalid utf-8") from exc
    return bytes(raw), size


class _ParseResult(Enum):
    """Outcome of one ``_try_parse_record_at`` — the scanner branches on THIS enum, never
    on catching an exception (expected variants, per the no-exceptions-as-control-flow
    rule). ``CEILING_HIT`` is kept distinct from ``INVALID`` for forensic accounting:
    ``INVALID`` is ordinary garbage at this offset, while ``CEILING_HIT`` is a clean
    header whose implied record exceeded ``max_payload`` (an overflow chain or unusually
    wide row) and so was dropped — logged separately so an analyst learns evidence may
    have been skipped."""

    SUCCESS = "success"
    INVALID = "invalid"
    CEILING_HIT = "ceiling_hit"


def _try_parse_record_at(
    buf: bytes, offset: int, schema: _TableSchema, max_payload: int,
) -> tuple[_ParseResult, dict[str, Any] | None, int]:
    """Try to read ``buf[offset:]`` as one record matching ``schema``.

    Returns ``(result, record, consumed)``: on SUCCESS, ``record`` is the row dict and
    ``consumed`` the header+body byte length; otherwise ``record`` is ``None`` and
    ``consumed`` 0. A truncated varint / body raised inside is caught here and reported
    as the ``INVALID`` variant — during a byte-scan that is just "no record starts here".
    """
    try:
        header_size, hsz_len = _read_varint(buf, offset)
    except ValueError:
        return (_ParseResult.INVALID, None, 0)
    if header_size <= 0 or header_size > len(buf) - offset:
        return (_ParseResult.INVALID, None, 0)
    if header_size > max_payload:
        return (_ParseResult.CEILING_HIT, None, 0)

    serials: list[int] = []
    pos = offset + hsz_len
    header_end = offset + header_size
    while pos < header_end:
        try:
            serial, slen = _read_varint(buf, pos)
        except ValueError:
            return (_ParseResult.INVALID, None, 0)
        serials.append(serial)
        pos += slen
    if pos != header_end:  # serial varints must land exactly on the header boundary
        return (_ParseResult.INVALID, None, 0)
    if len(serials) != schema.n_cols:  # wrong column count => not this table's row
        return (_ParseResult.INVALID, None, 0)

    for serial, column in zip(serials, schema.columns):
        if column.is_int_pk:
            # An INTEGER PRIMARY KEY stores NULL in the record body (the rowid varint
            # carries the value), so allow NULL or any integer serial here.
            if not (serial == 0 or serial in (1, 2, 3, 4, 5, 6, 8, 9)):
                return (_ParseResult.INVALID, None, 0)
        elif not _serial_compatible_with_affinity(serial, column.affinity):
            return (_ParseResult.INVALID, None, 0)

    values: list[Any] = []
    body_pos = header_end
    for serial in serials:
        try:
            value, used = _decode_value(buf, body_pos, serial)
        except ValueError:
            return (_ParseResult.INVALID, None, 0)
        values.append(value)
        body_pos += used
        if body_pos - offset > max_payload:
            return (_ParseResult.CEILING_HIT, None, 0)

    record = {column.name: value for column, value in zip(schema.columns, values)}
    return (_ParseResult.SUCCESS, record, body_pos - offset)


# --- carving driver -----------------------------------------------------------------

def _scan_region(
    region: bytes, schema: _TableSchema, source_name: str, max_payload: int,
) -> list[dict[str, Any]]:
    """Scan one free byte region left-to-right for records; tag each with ``source_name``.

    Overlap policy (deliberate, precision-over-recall): once a record is accepted at
    offset ``i`` spanning ``consumed`` bytes, those bytes are marked seen and no later
    record may start inside that window. This biases towards the FIRST plausible parse
    and suppresses overlapping candidates — fewer false positives at the cost of some
    real overlapping rows.
    """
    out: list[dict[str, Any]] = []
    consumed_until = 0  # offsets below this were swallowed by an accepted record
    ceiling_hits = 0
    i = 0
    while i < len(region):
        if i < consumed_until:
            i += 1
            continue
        result, record, consumed = _try_parse_record_at(region, i, schema, max_payload)
        if result is _ParseResult.CEILING_HIT:
            ceiling_hits += 1
            i += 1
            continue
        if result is _ParseResult.INVALID:
            i += 1
            continue
        assert record is not None, "SUCCESS must carry a record"
        out.append({**record, CARVE_SOURCE_COLNAME: source_name})
        consumed_until = i + consumed
        i += max(1, consumed)
    if ceiling_hits:
        log.warning(
            f"carve: dropped {ceiling_hits} {source_name} candidate(s) exceeding "
            f"max_payload={max_payload} — possible overflow records not recovered"
        )
    return out


@contextmanager
def _mapped(source: Path) -> Iterator[bytes | mmap.mmap]:
    """Memory-map ``source`` read-only so a multi-GB evidence file is carved without
    being pulled wholly into RAM. An empty file cannot be mmapped, so it short-circuits
    to empty bytes."""
    size = source.stat().st_size
    if size == 0:
        yield b""
        return
    with open(source, "rb") as handle:
        mapping = mmap.mmap(handle.fileno(), length=size, access=mmap.ACCESS_READ)
        try:
            yield mapping
        finally:
            mapping.close()


def carve_table(
    source: Path, table: str, columns: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Carve deleted rows of ``table`` from ``source``'s freelist and free space.

    Returns carved rows in the pinned scan order (ascending page number; within a page,
    unallocated gap then freeblock chain; within a region, ascending offset). Each row is
    a ``{column: value}`` dict plus ``CARVE_SOURCE_COLNAME`` (``FREELIST_SOURCE_NAME`` /
    ``FREESPACE_SOURCE_NAME``). The determinism of that order is what lets the reader
    assign each carved row a run-stable ``line_no``.

    ``columns``, when given, is the reader's live column list: if the carve schema's
    column names differ from it (a generated/hidden-column edge case) the carve is
    skipped and ``[]`` returned, so a carved row can never disagree in shape with the
    live rows it is numbered alongside.
    """
    schema = _read_table_schema(source, table)
    schema_columns = [c.name for c in schema.columns]
    if columns is not None and schema_columns != list(columns):
        log.debug(
            f"carve skipped for {table!r}: schema columns {schema_columns} != "
            f"live columns {list(columns)}"
        )
        return []

    carved: list[dict[str, Any]] = []
    with _mapped(source) as data:
        if len(data) < _DB_HEADER_SIZE:
            return []
        info = _parse_file_header(data)
        # A generous ceiling: overflow-page reassembly is out of scope, so a record whose
        # body would exceed two pages is dropped (and counted) rather than mis-carved.
        max_payload = info.page_size * 2
        freelist = _collect_freelist_pages(data, info)

        # Single ascending-page pass = the pinned, deterministic scan order. A freelist
        # page is carved whole (its old content is entirely unallocated); any other page
        # contributes only its leaf-table free-space regions.
        for page_no in range(1, info.total_pages + 1):
            page = _get_page(data, page_no, info.page_size)
            if not page:
                continue
            if page_no in freelist:
                carved.extend(
                    _scan_region(page, schema, FREELIST_SOURCE_NAME, max_payload)
                )
                continue
            for region, _offset_in_page in _leaf_table_freespace_regions(
                page, is_page_1=(page_no == 1),
            ):
                carved.extend(
                    _scan_region(region, schema, FREESPACE_SOURCE_NAME, max_payload)
                )
    return carved
