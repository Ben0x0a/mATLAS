"""Staging result types for the Container layer.

Defines:    StagedFile (one materialised file the reader opens) and StagedGroup (a
            SQLite db plus its co-located WAL/SHM/journal siblings in one temp dir).
Used by:    Container implementations (which produce them in ``stage``/``stage_group``)
            and the FormatReaders (which consume them).
Depends on: standard library + the SourceFile type (for the ``origin`` back-pointer).

WHY one place: forensic integrity requires the parser never opens the original
evidence — only a throwaway copy. ``integrity`` keeps the keys today's metadata uses
(``mode``/``ok``/``source_hash_before``/``verification_after``) so traceability is
unchanged. ``temp_dir`` is the directory to delete once the reader has finished.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from model_atlas.model.families import SourceTier

if TYPE_CHECKING:
    from model_atlas.sources.container import SourceFile

# How a reader's source is materialised, decided per (reader, source tier):
#   ALWAYS  copy to a temp file always (a technical need, e.g. SQLite's WAL two-pass merge);
#   NEVER   read in place always (e.g. a multi-GB XML report — never copied);
#   TIER    copy only for PRIMARY-tier evidence, read in place otherwise.
# Forensic rationale: primary (first-hand) evidence earns the full copy + before/after hash
# chain; secondary tool exports are read in place to avoid needless multi-GB copies.
STAGING_ALWAYS = "always"
STAGING_NEVER = "never"
STAGING_TIER = "tier"


def should_copy(staging_mode: str, tier: str | None) -> bool:
    """Whether a source should be copied to a temp file (vs read in place)."""
    if staging_mode == STAGING_ALWAYS:
        return True
    if staging_mode == STAGING_NEVER:
        return False
    return tier == SourceTier.PRIMARY.value  # STAGING_TIER


@dataclass(frozen=True)
class StagedFile:
    path: Path                       # the temp copy a reader opens
    fingerprint: str                 # sha256 of THIS file's content bytes
    origin: "SourceFile"
    integrity: dict[str, Any] = field(default_factory=dict)
    temp_dir: Path | None = None     # directory to clean up after the reader is done


@dataclass(frozen=True)
class StagedGroup:
    dir: Path                        # temp dir holding the db + siblings
    members: dict[str, Path]         # {"db", "wal", "shm", "journal"} -> path (present only)
    fingerprint: str                 # content hash of the db member
    origin: "SourceFile"
    integrity: dict[str, Any] = field(default_factory=dict)
    temp_dir: Path | None = None
