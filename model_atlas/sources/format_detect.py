"""Content-based (magic) format detection for a claimed source file.

Defines:    detect_format — a magic-first ladder that classifies a file's head bytes
            (with an optional zip central-directory peek) into a format string.
Used by:    presets.matcher (the type guard) and discover (recognising an archive).
Depends on: standard library only.

Magic bytes always win; the suffix is consulted only for CSV/TSV, which have no
reliable signature. ``plist``/``xml``/``archive`` are recognised for classification and
skipping even though this task ships no reader for them.
"""
from __future__ import annotations

from typing import Callable

SQLITE_MAGIC = b"SQLite format 3\x00"
BPLIST_MAGIC = b"bplist00"
ZIP_MAGIC = b"PK\x03\x04"

# Formats a preset may target (a reader exists for each). ``xml`` covers forensic XML
# reports (Cellebrite UFED, AXIOM, …), discriminated further by namespace in the selector.
TARGETABLE_FORMATS: frozenset[str] = frozenset({"csv", "excel", "sqlite", "xml"})


def _looks_like_excel(names: list[str]) -> bool:
    """A zip is an xlsx workbook when it carries the OOXML spreadsheet part."""
    has_content_types = "[Content_Types].xml" in names
    for name in names:
        if name == "xl/workbook.xml":
            return True
        if name.startswith("xl/") and has_content_types:
            return True
    return False


def _strip_bom(head: bytes) -> bytes:
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            return head[len(bom):]
    return head


def detect_format(
    head: bytes,
    peek_zip: Callable[[], list[str]] | None,
    suffix: str,
) -> str | None:
    """Classify a file from its first bytes, an optional zip-entry peek, and its suffix.

    Returns one of ``sqlite``/``excel``/``csv`` (readable), ``plist``/``xml``/``archive``
    (classified for skip/recursion), or ``None`` when unknown (caller warns + skips).
    """
    if head[:16] == SQLITE_MAGIC:
        return "sqlite"
    if head[:8] == BPLIST_MAGIC:
        return "plist"
    if head[:4] == ZIP_MAGIC:
        names = peek_zip() if peek_zip is not None else []
        return "excel" if _looks_like_excel(names) else "archive"

    text = _strip_bom(head).lstrip()
    if text.startswith(b"<?xml") or text.startswith(b"<"):
        window = text[:512].lower()
        if b"<!doctype plist" in window or b"<plist" in window:
            return "plist"
        return "xml"

    lowered = suffix.lower()
    if lowered in (".csv", ".tsv"):
        return "csv"
    return None
