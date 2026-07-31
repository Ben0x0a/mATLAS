"""Tests for header-first format detection (model_atlas.ingest.format_detect).

``detect_format`` classifies a file from its magic bytes (+ an optional zip-entry peek and
the suffix as a last resort) — the forensic "trust the content, not the name" rule. These
exercise every classification branch and the graceful-degradation paths.
Used by:    pytest.
Depends on: model_atlas.ingest.format_detect, the standard library.
"""
from __future__ import annotations

import io

from model_atlas.ingest.format_detect import (
    BPLIST_MAGIC,
    SQLITE_MAGIC,
    ZIP_MAGIC,
    _looks_like_excel,
    _strip_bom,
    _zip_names,
    detect_format,
)


def test_sqlite_magic_wins_over_suffix() -> None:
    # Content beats the name: a SQLite header labelled .csv is still SQLite.
    assert detect_format(SQLITE_MAGIC + b"\x00" * 100, None, ".csv") == "sqlite"


def test_binary_plist_detected() -> None:
    assert detect_format(BPLIST_MAGIC + b"\x00\x08", None, ".bin") == "plist"


def test_zip_with_ooxml_parts_is_excel_otherwise_archive() -> None:
    excel_names = ["[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml"]
    assert detect_format(ZIP_MAGIC + b"...", lambda: excel_names, ".xlsx") == "excel"
    assert detect_format(ZIP_MAGIC + b"...", lambda: ["a.txt", "b/c.csv"], ".zip") == "archive"
    # No peek available -> a bare PK file classifies as a (recursable) archive, never excel.
    assert detect_format(ZIP_MAGIC + b"...", None, ".zip") == "archive"


def test_xml_plist_vs_plain_xml() -> None:
    assert detect_format(b'<?xml version="1.0"?><plist><dict/></plist>', None, ".plist") == "plist"
    assert detect_format(b"<root><child/></root>", None, ".xml") == "xml"


def test_bom_prefixed_xml_is_still_classified() -> None:
    # A UTF-8 BOM before the declaration must not hide the XML.
    assert detect_format(b"\xef\xbb\xbf<?xml version='1.0'?><a/>", None, ".xml") == "xml"


def test_csv_by_suffix_only_and_unknown_is_none() -> None:
    assert detect_format(b"a,b,c\n1,2,3\n", None, ".csv") == "csv"
    assert detect_format(b"a,b,c\n1,2,3\n", None, ".tsv") == "csv"
    # No magic, not a csv/tsv suffix -> unknown (the caller warns + skips, never guesses).
    assert detect_format(b"just some bytes", None, ".dat") is None


def test_leading_angle_bracket_csv_is_not_misrouted_to_xml() -> None:
    # FIX-08 #5: a .csv whose first field merely starts with "<" is NOT XML — it must classify
    # csv, not be handed to the XML reader (which would raise and drop a readable CSV).
    assert detect_format(b"<Location>,<Item ID>\nA,1\n", None, ".csv") == "csv"
    # A genuinely namespaced root, or an XML declaration, still classifies as xml (content wins).
    assert detect_format(b'<project xmlns="urn:x">...', None, ".xml") == "xml"
    assert detect_format(b'<?xml version="1.0"?><root/>', None, ".csv") == "xml"
    # A "<"-leading markup file with no csv/tsv suffix stays xml (surfaced, not dropped).
    assert detect_format(b"<root><child/></root>", None, ".dat") == "xml"


def test_strip_bom_removes_each_known_bom() -> None:
    assert _strip_bom(b"\xef\xbb\xbfhi") == b"hi"          # UTF-8
    assert _strip_bom(b"\xff\xfehi") == b"hi"              # UTF-16 LE
    assert _strip_bom(b"\xfe\xffhi") == b"hi"              # UTF-16 BE
    assert _strip_bom(b"nohere") == b"nohere"


def test_looks_like_excel_requires_the_authoritative_workbook_part() -> None:
    # FIX-08 #8: only the authoritative xl/workbook.xml marks a workbook. A [Content_Types].xml
    # plus SOME other xl/ part is NOT enough — that loose test misclassified a non-workbook or
    # partial OOXML archive as excel, and its leaf files were then never walked (a silent loss).
    assert _looks_like_excel(["xl/workbook.xml"]) is True                  # the workbook part alone
    assert _looks_like_excel(["[Content_Types].xml", "xl/workbook.xml"]) is True
    assert _looks_like_excel(["[Content_Types].xml", "xl/styles.xml"]) is False  # no workbook part
    assert _looks_like_excel(["xl/styles.xml"]) is False                  # an xl/ part but no workbook
    assert _looks_like_excel(["word/document.xml"]) is False              # a docx, not a workbook


def test_zip_names_degrades_to_empty_on_a_non_zip_stream() -> None:
    # A PK-less / corrupt stream must not raise — detection degrades to "archive", not a crash.
    assert _zip_names(lambda: io.BytesIO(b"not a zip at all")) == []
