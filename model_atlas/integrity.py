"""Source-agnostic integrity helpers."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    log.debug("Computing SHA-256 for %s", path)
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    log.debug("SHA-256 computed for %s: %s", path, digest)
    return digest
