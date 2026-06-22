"""Tests for the magic-first format ladder (sources.format_detect)."""
from __future__ import annotations

from model_atlas.sources.format_detect import detect_format


def test_sqlite_magic() -> None:
    assert detect_format(b"SQLite format 3\x00rest", None, ".db") == "sqlite"


def test_bplist_magic() -> None:
    assert detect_format(b"bplist00\x00\x00", None, ".plist") == "plist"


def test_zip_with_workbook_is_excel() -> None:
    names = ["[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml"]
    assert detect_format(b"PK\x03\x04rest", lambda: names, ".xlsx") == "excel"


def test_zip_without_workbook_is_archive() -> None:
    names = ["filesystem1/private/a.sqlite", "a.txt"]
    assert detect_format(b"PK\x03\x04rest", lambda: names, ".zip") == "archive"


def test_xml_plist_doctype() -> None:
    head = b'<?xml version="1.0"?>\n<!DOCTYPE plist PUBLIC>'
    assert detect_format(head, None, ".plist") == "plist"


def test_xml_plist_root() -> None:
    assert detect_format(b"<plist version=\"1.0\">", None, ".xml") == "plist"


def test_generic_xml() -> None:
    assert detect_format(b"<?xml version=\"1.0\"?><root/>", None, ".xml") == "xml"


def test_csv_by_suffix() -> None:
    assert detect_format(b"Lat,Lon,TS\n1,2,3", None, ".csv") == "csv"
    assert detect_format(b"a\tb\tc\n", None, ".tsv") == "csv"


def test_unknown_returns_none() -> None:
    assert detect_format(b"\x00\x01\x02binary", None, ".bin") is None


def test_utf8_bom_text_is_xml() -> None:
    assert detect_format(b"\xef\xbb\xbf<?xml ?>", None, ".xml") == "xml"


def test_tsv_by_suffix() -> None:
    assert detect_format(b"a\tb\tc", None, ".tsv") == "csv"


def test_empty_head_is_unknown() -> None:
    assert detect_format(b"", None, "") is None


def test_truncated_sqlite_magic_is_not_sqlite() -> None:
    # 15 bytes — missing the trailing NUL of the 16-byte signature.
    assert detect_format(b"SQLite format 3", None, "") is None


def test_zip_without_workbook_part_is_archive() -> None:
    assert detect_format(b"PK\x03\x04rest", lambda: ["a/b.txt", "c.bin"], ".zip") == "archive"


def test_suffix_does_not_override_magic() -> None:
    # A real SQLite file mislabelled .csv is still sqlite (magic wins).
    assert detect_format(b"SQLite format 3\x00", None, ".csv") == "sqlite"
