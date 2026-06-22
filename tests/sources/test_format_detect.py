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
