"""Unit tests for the carver's pure primitives (``model_atlas.sqlite.carve`` internals).

Covers the edge/error branches the end-to-end fixtures in test_carve.py do not reach: the
varint limits, the file-header validation, the type-affinity algorithm, the serial-type
sizing + affinity plausibility gate, per-value decoding (sign extension, constants,
invalid-UTF8, truncation, reserved types), the record parser's SUCCESS / INVALID /
CEILING_HIT outcomes, and the region scanner's first-match overlap policy. These are the
forensic-critical decode paths of a binary carver — a mis-decode is silent evidence
corruption, so every branch is pinned here rather than left to the happy-path fixtures.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest

from model_atlas.sqlite.carve import (
    carve_table,
    _AFF_BLOB,
    _AFF_INTEGER,
    _AFF_NUMERIC,
    _AFF_REAL,
    _AFF_TEXT,
    _HEADER_MAGIC,
    _ColumnSchema,
    _ParseResult,
    _TableSchema,
    _affinity,
    _decode_value,
    _mapped,
    _parse_file_header,
    _read_varint,
    _scan_region,
    _serial_compatible_with_affinity,
    _serial_type_size,
    _try_parse_record_at,
)


# --- varint -------------------------------------------------------------------

def test_varint_single_byte() -> None:
    assert _read_varint(b"\x05", 0) == (5, 1)


def test_varint_two_byte_continuation() -> None:
    # 0x81 sets the continuation bit (low 7 = 1); 0x00 ends it: (1<<7)|0 = 128.
    assert _read_varint(b"\x81\x00", 0) == (128, 2)


def test_varint_full_nine_bytes_uses_all_eight_bits_of_the_ninth() -> None:
    # Eight continuation bytes then a ninth that contributes ALL 8 bits (the 64-bit span).
    value, consumed = _read_varint(b"\x81" * 8 + b"\x7f", 0)
    assert consumed == 9


def test_varint_truncated_raises() -> None:
    # A lone continuation byte with nothing after it cannot complete: a loud ValueError
    # the scanner maps to "no record here" rather than a silent mis-read.
    with pytest.raises(ValueError):
        _read_varint(b"\x81", 0)


def test_varint_ninth_byte_missing_raises() -> None:
    with pytest.raises(ValueError):
        _read_varint(b"\x81" * 8, 0)   # 8 continuations, no ninth byte


# --- file header --------------------------------------------------------------

def _header(page_size: int, freelist_trunk: int = 0, total_len: int | None = None) -> bytes:
    buf = bytearray(total_len if total_len is not None else 100)
    buf[0:16] = _HEADER_MAGIC
    struct.pack_into(">H", buf, 16, page_size if page_size != 65536 else 1)
    struct.pack_into(">I", buf, 32, freelist_trunk)
    return bytes(buf)


def test_parse_header_rejects_non_sqlite() -> None:
    with pytest.raises(ValueError):
        _parse_file_header(b"\x00" * 100)


def test_parse_header_page_size_and_page_count() -> None:
    info = _parse_file_header(_header(4096, freelist_trunk=7, total_len=4096 * 3))
    assert info.page_size == 4096
    assert info.first_freelist_trunk == 7
    assert info.total_pages == 3


def test_parse_header_stored_one_means_65536() -> None:
    # §1.3: a page-size field of 1 encodes 65536 (does not fit in u16).
    info = _parse_file_header(_header(65536, total_len=65536))
    assert info.page_size == 65536


# --- type affinity (§3.1) -----------------------------------------------------

@pytest.mark.parametrize("declared, expected", [
    ("INTEGER", _AFF_INTEGER), ("BIGINT", _AFF_INTEGER),
    ("VARCHAR(10)", _AFF_TEXT), ("CLOB", _AFF_TEXT), ("TEXT", _AFF_TEXT),
    ("BLOB", _AFF_BLOB), ("", _AFF_BLOB),
    ("REAL", _AFF_REAL), ("FLOAT", _AFF_REAL), ("DOUBLE", _AFF_REAL),
    ("NUMERIC", _AFF_NUMERIC), ("DECIMAL(5,2)", _AFF_NUMERIC),
])
def test_affinity(declared: str, expected: str) -> None:
    assert _affinity(declared) == expected


# --- serial-type sizing -------------------------------------------------------

@pytest.mark.parametrize("serial, size", [
    (0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 6), (6, 8), (7, 8), (8, 0), (9, 0),
    (12, 0), (14, 1), (16, 2),        # BLOB of (serial-12)/2 bytes
    (13, 0), (15, 1), (17, 2),        # TEXT of (serial-13)/2 bytes
    (10, -1), (11, -1),               # reserved for internal use
])
def test_serial_type_size(serial: int, size: int) -> None:
    assert _serial_type_size(serial) == size


# --- affinity plausibility gate ----------------------------------------------

def test_serial_compat_null_always_ok() -> None:
    for aff in (_AFF_INTEGER, _AFF_REAL, _AFF_TEXT, _AFF_BLOB, _AFF_NUMERIC):
        assert _serial_compatible_with_affinity(0, aff)


def test_serial_compat_integer_rejects_float_and_text() -> None:
    assert _serial_compatible_with_affinity(1, _AFF_INTEGER)
    assert not _serial_compatible_with_affinity(7, _AFF_INTEGER)    # a float serial
    assert not _serial_compatible_with_affinity(13, _AFF_INTEGER)   # a text serial


def test_serial_compat_real_accepts_numeric_serials() -> None:
    assert _serial_compatible_with_affinity(7, _AFF_REAL)
    assert not _serial_compatible_with_affinity(13, _AFF_REAL)


def test_serial_compat_text_only_odd_from_13() -> None:
    assert _serial_compatible_with_affinity(15, _AFF_TEXT)
    assert not _serial_compatible_with_affinity(14, _AFF_TEXT)      # an even (BLOB) serial
    assert not _serial_compatible_with_affinity(1, _AFF_TEXT)


def test_serial_compat_blob_rejects_reserved_only() -> None:
    assert _serial_compatible_with_affinity(14, _AFF_BLOB)
    assert not _serial_compatible_with_affinity(10, _AFF_BLOB)


def test_serial_compat_numeric_accepts_numbers_and_text_not_blob() -> None:
    assert _serial_compatible_with_affinity(6, _AFF_NUMERIC)
    assert _serial_compatible_with_affinity(15, _AFF_NUMERIC)       # text is numeric-ish
    assert not _serial_compatible_with_affinity(14, _AFF_NUMERIC)   # a BLOB is not


def test_serial_compat_unknown_affinity_is_false() -> None:
    # A defensive fallthrough: an affinity the gate does not recognise rejects everything.
    assert not _serial_compatible_with_affinity(1, "MADE_UP_AFFINITY")


# --- value decoding -----------------------------------------------------------

def test_decode_int_sign_extends_negative() -> None:
    # serial 1 (1-byte int) 0x80 is -128 two's-complement: the sign must extend to 8 bytes.
    assert _decode_value(b"\x80", 0, 1) == (-128, 1)


def test_decode_three_byte_int() -> None:
    assert _decode_value(b"\x00\x01\x00", 0, 3) == (256, 3)


def test_decode_float() -> None:
    raw = struct.pack(">d", 1.5)
    assert _decode_value(raw, 0, 7) == (1.5, 8)


def test_decode_constants_consume_no_bytes() -> None:
    assert _decode_value(b"", 0, 8) == (0, 0)    # constant 0
    assert _decode_value(b"", 0, 9) == (1, 0)    # constant 1


def test_decode_text_and_blob() -> None:
    assert _decode_value(b"hi", 0, 17) == ("hi", 2)     # TEXT of 2 bytes
    assert _decode_value(b"\xaa\xbb", 0, 16) == (b"\xaa\xbb", 2)   # BLOB of 2 bytes


def test_decode_invalid_utf8_raises() -> None:
    # A "text" cell that is not valid UTF-8 is almost certainly a mis-aligned false
    # positive, so it is rejected rather than lossily replaced.
    with pytest.raises(ValueError):
        _decode_value(b"\xff", 0, 15)


def test_decode_truncated_raises() -> None:
    with pytest.raises(ValueError):
        _decode_value(b"", 0, 1)       # a 1-byte int with no byte present


def test_decode_reserved_serial_raises() -> None:
    with pytest.raises(ValueError):
        _decode_value(b"", 0, 10)


# --- record parsing -----------------------------------------------------------

def _varint(n: int) -> bytes:
    """Encode a non-negative int as a SQLite big-endian varint (test-side, mirrors decode)."""
    if n == 0:
        return b"\x00"
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def _record(serials: list[int], body: bytes) -> bytes:
    """Encode a SQLite record: varint(header_len) + serial varints + body.

    Assumes the header-length varint itself fits in one byte (header < 128 bytes), which
    holds for every record these tests build.
    """
    serial_bytes = b"".join(_varint(s) for s in serials)
    header_len = 1 + len(serial_bytes)     # +1 for the single-byte header-length varint
    return _varint(header_len) + serial_bytes + body


def test_parse_record_success() -> None:
    schema = _TableSchema([_ColumnSchema("n", _AFF_INTEGER, False),
                           _ColumnSchema("t", _AFF_TEXT, False)])
    buf = _record([1, 15], b"\x2a" + b"x")   # int8=42, text of 1 byte "x"
    result, record, consumed = _try_parse_record_at(buf, 0, schema, max_payload=1000)
    assert result is _ParseResult.SUCCESS
    assert record == {"n": 42, "t": "x"}
    assert consumed == len(buf)


def test_parse_record_wrong_column_count_is_invalid() -> None:
    schema = _TableSchema([_ColumnSchema("n", _AFF_INTEGER, False)])   # 1 column
    buf = _record([1, 1], b"\x01\x02")                                 # 2 serials
    result, record, _ = _try_parse_record_at(buf, 0, schema, max_payload=1000)
    assert result is _ParseResult.INVALID and record is None


def test_parse_record_affinity_mismatch_is_invalid() -> None:
    schema = _TableSchema([_ColumnSchema("n", _AFF_INTEGER, False)])
    buf = _record([13], b"")           # a TEXT serial under an INTEGER column
    result, _, _ = _try_parse_record_at(buf, 0, schema, max_payload=1000)
    assert result is _ParseResult.INVALID


def test_parse_record_header_over_ceiling_is_ceiling_hit() -> None:
    schema = _TableSchema([_ColumnSchema("b", _AFF_BLOB, False)])
    # A header-length varint larger than max_payload but within the buffer -> CEILING_HIT
    # (a record too big to be a plausible carve, e.g. the head of an overflow chain).
    buf = bytes([50]) + b"\x00" * 99     # header_size = 50, buffer 100 bytes
    result, _, _ = _try_parse_record_at(buf, 0, schema, max_payload=5)
    assert result is _ParseResult.CEILING_HIT


def test_parse_record_body_over_ceiling_is_ceiling_hit() -> None:
    schema = _TableSchema([_ColumnSchema("b", _AFF_BLOB, False)])
    # A valid header (small) whose BLOB body (100 bytes, serial 12+2*100=212) overruns a
    # tiny max_payload during body decode -> the second CEILING_HIT branch.
    buf = _record([12 + 2 * 100], b"\x00" * 100)
    result, _, _ = _try_parse_record_at(buf, 0, schema, max_payload=50)
    assert result is _ParseResult.CEILING_HIT


def test_parse_record_int_pk_allows_null_body() -> None:
    # An INTEGER PRIMARY KEY stores NULL in the body (the rowid carries the value): a
    # serial 0 under the pk column must still parse.
    schema = _TableSchema([_ColumnSchema("id", _AFF_INTEGER, True),
                           _ColumnSchema("v", _AFF_INTEGER, False)])
    buf = _record([0, 1], b"\x09")     # pk NULL, v=9
    result, record, _ = _try_parse_record_at(buf, 0, schema, max_payload=1000)
    assert result is _ParseResult.SUCCESS and record == {"id": None, "v": 9}


# --- region scanning ----------------------------------------------------------

def test_scan_region_first_match_overlap_policy() -> None:
    # Two adjacent valid single-column int records; the scanner accepts the first and does
    # not re-parse inside its consumed span, so both are found exactly once, in order.
    schema = _TableSchema([_ColumnSchema("n", _AFF_INTEGER, False)])
    region = _record([1], b"\x2a") + _record([1], b"\x2b")   # 42 then 43
    out = _scan_region(region, schema, "freespace", max_payload=1000)
    assert [r["n"] for r in out] == [42, 43]
    assert all(r["_meta_sqlite_carve_source"] == "freespace" for r in out)


def test_scan_region_empty_when_nothing_parses() -> None:
    schema = _TableSchema([_ColumnSchema("n", _AFF_INTEGER, False)])
    assert _scan_region(b"\x00\x00\x00", schema, "freelist", max_payload=1000) == []


# --- mmap of an empty file ----------------------------------------------------

def test_mapped_empty_file_yields_empty_bytes(tmp_path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with _mapped(empty) as data:
        assert bytes(data) == b""


# --- carve_table schema guard -------------------------------------------------

def test_carve_table_unknown_table_raises(tmp_path) -> None:
    # The schema read is the first thing carving does; a table that is not in the file is a
    # loud ValueError, never a silent empty carve (which would hide a mis-named target).
    db = tmp_path / "x.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        carve_table(db, "does_not_exist")
