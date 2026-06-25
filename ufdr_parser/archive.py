"""Open the decoded ``report.xml`` from a .ufdr archive or a bare .xml file.

Defines:    open_report (context manager yielding a binary stream) and report_size.
Used by:    dump (the orchestrator) and the parallel parser.
Depends on: const (REPORT_ENTRY_NAME), standard library zipfile.

A .ufdr is a zip (usually STORED); the report is a single ``report.xml`` entry that can
be up to ~32 GB. We never extract it to disk — the stream is read incrementally by the
lxml iterparse reader. A plain ``.xml`` path is accepted too, matching the legacy tool.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterator

from ufdr_parser.const import REPORT_ENTRY_NAME

log = logging.getLogger(__name__)

_ZIP_MAGIC = b"PK\x03\x04"


def _is_zip(path: Path) -> bool:
    """Magic-first check: trust the header, not the extension (a .ufdr is a zip)."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == _ZIP_MAGIC
    except OSError:
        return False


@contextlib.contextmanager
def open_report(path: Path) -> Iterator[BinaryIO]:
    """Yield a readable binary stream over the report XML.

    A zip input is opened and its ``report.xml`` entry streamed; a non-zip input is
    treated as the report XML itself. The stream and any owning archive handle are
    closed on exit.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if _is_zip(path):
        zf = zipfile.ZipFile(path)
        try:
            if REPORT_ENTRY_NAME not in zf.namelist():
                raise ValueError(f"{path}: no {REPORT_ENTRY_NAME} entry in the archive")
            log.info(f"Streaming {REPORT_ENTRY_NAME} from archive {path}")
            with zf.open(REPORT_ENTRY_NAME) as stream:
                yield stream
        finally:
            zf.close()
    else:
        log.info(f"Reading report XML directly from {path}")
        with path.open("rb") as stream:
            yield stream


def report_size(path: Path) -> int:
    """Uncompressed size of the report XML in bytes (for progress / worker sizing)."""
    path = Path(path)
    if _is_zip(path):
        with zipfile.ZipFile(path) as zf:
            return zf.getinfo(REPORT_ENTRY_NAME).file_size
    return path.stat().st_size


# Access kinds for the seekable parallel-parse readers.
KIND_PLAIN = "plain"      # a bare report.xml file (or an extracted temp copy)
KIND_STORED = "stored"    # a STORED (uncompressed) zip entry — cheap random seek
KIND_DEFLATE = "deflate"  # a DEFLATE zip entry — seek decompresses from start (avoid)


def report_compression(path: Path) -> str:
    """Classify how report.xml is stored: ``plain`` / ``stored`` / ``deflate``.

    Parallel parsing seeks into the report; that is cheap for a plain file or a STORED zip
    entry, but a DEFLATE entry must be extracted first (see ``extract_report``).
    """
    path = Path(path)
    if not _is_zip(path):
        return KIND_PLAIN
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(REPORT_ENTRY_NAME)
        return KIND_STORED if info.compress_type == zipfile.ZIP_STORED else KIND_DEFLATE


@contextlib.contextmanager
def open_seekable(path: Path, kind: str) -> Iterator[BinaryIO]:
    """Yield a seekable binary stream over report.xml for the given access ``kind``.

    Each call opens an independent handle (so parallel worker processes can seek their own
    ranges). ``deflate`` is unsupported here — extract first and read the copy as ``plain``.
    """
    path = Path(path)
    if kind == KIND_PLAIN:
        with path.open("rb") as stream:
            yield stream
    elif kind == KIND_STORED:
        with zipfile.ZipFile(path) as zf, zf.open(REPORT_ENTRY_NAME) as stream:
            yield stream
    else:
        raise ValueError(f"open_seekable does not support kind={kind!r}; extract first")


def extract_report(path: Path, dest: Path) -> Path:
    """Stream report.xml out of a zip to ``dest`` (used for DEFLATE archives)."""
    path, dest = Path(path), Path(dest)
    with zipfile.ZipFile(path) as zf, zf.open(REPORT_ENTRY_NAME) as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out, length=8 * 1024 * 1024)
    return dest
