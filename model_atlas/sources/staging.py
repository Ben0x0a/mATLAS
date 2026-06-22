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

if TYPE_CHECKING:
    from model_atlas.sources.container import SourceFile


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
