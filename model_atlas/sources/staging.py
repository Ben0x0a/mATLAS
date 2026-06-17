"""Stage a source file into a throwaway temp copy before extraction.

Defines:    StagedFile (the staging result) and stage_file (a context manager that
            hashes the original, copies it to a temp dir, and yields the local copy).
Used by:    the CSV and Excel source adapters.
Depends on: integrity (sha256_file); standard library only otherwise.

WHY: forensic integrity requires that the parser never opens — let alone touches —
the original evidence file. The SQLite adapter already works on a copy (via
``sqlite.locate``); this gives CSV/Excel the same guarantee through one shared helper.
"""
from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from model_atlas.integrity import sha256_file

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StagedFile:
    original: Path      # the untouched source on disk
    staged: Path        # the temp copy the parser reads
    sha256: str         # SHA-256 of the original, computed before copying


@contextlib.contextmanager
def stage_file(path: Path) -> Iterator[StagedFile]:
    """Hash ``path``, copy it into a temp dir, and yield the local copy.

    The temp dir (and the copy) live for the duration of the ``with`` block, so the
    caller must finish reading before leaving it. The original is only read, never
    written, so re-hashing it afterwards (the adapter's integrity check) always
    confirms it is unchanged.
    """
    original = Path(path)
    sha = sha256_file(original)
    with tempfile.TemporaryDirectory(prefix="matlas-stage-") as td:
        staged = Path(td) / original.name
        # copy2 preserves mtime/permissions so the copy is a faithful working replica.
        shutil.copy2(original, staged)
        log.debug("Staged source %s -> %s (sha256=%s)", original, staged, sha)
        yield StagedFile(original=original, staged=staged, sha256=sha)
