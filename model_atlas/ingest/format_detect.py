"""Content-based (magic) format detection for a claimed source file (L0 writer copy).

Defines:    ``detect_format`` — a magic-first ladder that classifies a file's head bytes
            (with an optional zip central-directory peek) into a format string; and the
            two wrappers that feed it — ``detect_source_format`` (a ``SourceFile``) and
            ``detect_path_format`` (an on-disk ``Path``). The single source of truth for
            magic detection inside the ingest engine.
Used by:    ``model_atlas.ingest.discover`` (an archive-vs-leaf test during traversal)
            and ``model_atlas.ingest.writer`` (choosing a reader for a leaf file).
Depends on: ``model_atlas.ingest.containers`` (``SourceFile``) and the standard library
            only.

Ported verbatim from ``sources/format_detect.py``: magic bytes always win;
the suffix is consulted only for CSV/TSV, which have no reliable signature.
``plist``/``xml``/``archive`` are recognised for classification and skip/recursion even
where no reader ships for them.
"""
from __future__ import annotations

import logging
import zipfile
from typing import TYPE_CHECKING, BinaryIO, Callable

if TYPE_CHECKING:
    from pathlib import Path

    from model_atlas.ingest.containers import SourceFile

log = logging.getLogger(__name__)

# One head-read size shared by both wrappers. The magic ladder only needs 16 bytes for the
# SQLite/bplist/zip signatures, but the text branch scans a 512-byte window for the
# ``<plist``/``<!doctype plist`` marker; 512 is the safe superset that covers both.
_HEAD_BYTES = 512

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


def _zip_names(open_fn: Callable[[], BinaryIO]) -> list[str]:
    """Entry names of an archive, opened via ``open_fn`` (a fresh binary stream per call).

    Returns ``[]`` on any open/read failure so detection degrades gracefully (a PK file
    whose central directory cannot be listed is classified ``archive`` rather than raising).
    """
    try:
        with open_fn() as fh:
            with zipfile.ZipFile(fh) as zf:
                return zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return []


def detect_source_format(file: "SourceFile") -> str | None:
    """Open a claimed SourceFile once and magic-detect its format (head + zip-entry peek)."""
    container = file.container
    try:
        with container.open(file) as fh:
            head = fh.read(_HEAD_BYTES)
    except (OSError, KeyError, zipfile.BadZipFile):
        log.debug(f"Could not read {file.logical_path} for format detection", exc_info=True)
        return None
    return detect_format(head, lambda: _zip_names(lambda: container.open(file)), file.logical_path.suffix)


def detect_path_format(path: "Path") -> str | None:
    """Magic-detect the format of an on-disk file (to tell an input archive from a leaf file)."""
    with path.open("rb") as fh:
        head = fh.read(_HEAD_BYTES)
    return detect_format(head, lambda: _zip_names(lambda: path.open("rb")), path.suffix)
