"""SQLite source adapter configuration."""
from __future__ import annotations

from enum import Enum


class SourceHashMode(str, Enum):
    """How the source archive's integrity is captured.

    STRATEGIC (default for zips) hashes only the central directory and the
    extracted entries' spans/content — cheap on huge archives. FULL hashes the
    whole file twice (before/after); it is always used for direct SQLite
    sources and is the opt-in mode for zips when a single whole-file digest is
    required. See ``model_atlas/sqlite/integrity.py``.

    NONE skips integrity capture entirely for internal metadata reads.
    """
    STRATEGIC = "strategic"
    FULL = "full"
    NONE = "none"
