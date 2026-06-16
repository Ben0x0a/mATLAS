"""Locate and copy a SQLite database (with its WAL/SHM/journal siblings) from a
source — either a ZIP archive or a direct SQLite file path — into a working
directory, without modifying the source.
"""
from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from model_atlas.sqlite.config import SourceHashMode
from model_atlas.sqlite.integrity import ZipFingerprint, fingerprint_zip, snapshot

log = logging.getLogger(__name__)

WAL_SUFFIX = "-wal"
SHM_SUFFIX = "-shm"
JOURNAL_SUFFIX = "-journal"
SQLITE_HEADER = b"SQLite format 3\x00"

SIDECAR_SUFFIXES = (WAL_SUFFIX, SHM_SUFFIX, JOURNAL_SUFFIX)


@dataclass
class LocatedDB:
    tmp_db_path: Path
    wal_path: Optional[Path]
    shm_path: Optional[Path]
    journal_path: Optional[Path]
    source_paths: Dict[str, Path]      # name -> original path (for hash verify after run)
    source_hashes: Dict[str, str]      # name -> sha256 computed BEFORE (FULL mode / direct files)
    source_fingerprint: Optional[ZipFingerprint] = None  # set for STRATEGIC zip mode


def _copy_zip_entry(zipf: zipfile.ZipFile, entry: zipfile.ZipInfo, dest: Path) -> None:
    with zipf.open(entry) as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)


def is_sqlite_database(path: Path) -> bool:
    """Return True when a file starts with the SQLite database header."""
    path = Path(path)
    if not path.is_file() or path.name.endswith(SIDECAR_SUFFIXES):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def is_zip_archive(path: Path) -> bool:
    """Return True when a file is a ZIP archive, independent of extension."""
    path = Path(path)
    return path.is_file() and zipfile.is_zipfile(path)


def _normalise_relpath(relpath: Path) -> str:
    """Normalise a user-supplied internal path for matching ZIP entries:
    POSIX separators, no leading slash. So a Windows-style "subdir\\db.sqlite"
    and a UNIX-absolute-looking "/private/.../db.sqlite" both match the entry
    stored as "private/.../db.sqlite"."""
    return str(relpath).replace("\\", "/").lstrip("/")


def _locate_in_zip(
    source: Path, db_relpath: Path, tmpdir: Path, mode: SourceHashMode
) -> LocatedDB:
    rel = _normalise_relpath(db_relpath)
    db_name = Path(rel).name

    source_paths = {"zip": source}

    found: Dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(source, "r") as zipf:
        # Map every entry by its normalised path, and collect top-level folder
        # names. Acquisition tools often wrap the filesystem in a single root
        # folder (e.g. "filesystem1/", "Dump/", "_/"), so if the requested path
        # is not present verbatim we also try it under each root (ONE level
        # only — we never search deeper).
        entries: Dict[str, zipfile.ZipInfo] = {}
        roots: set[str] = set()
        for info in zipf.infolist():
            norm = info.filename.replace("\\", "/").lstrip("/")
            entries[norm] = info
            head, sep, _ = norm.partition("/")
            if sep and head:
                roots.add(head)

        candidates: List[str] = [rel] + [f"{root}/{rel}" for root in sorted(roots)]
        seen: set[str] = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]

        resolved_base: Optional[str] = None
        for base in candidates:
            db_info = entries.get(base)
            if db_info is None:
                continue
            resolved_base = base
            found["db"] = db_info
            for key, suffix in (("wal", WAL_SUFFIX), ("shm", SHM_SUFFIX), ("journal", JOURNAL_SUFFIX)):
                sib = entries.get(base + suffix)
                if sib is not None:
                    found[key] = sib
            break

        if resolved_base is None:
            raise FileNotFoundError(
                f"Database '{rel}' not found in {source} "
                f"(checked the path and {len(candidates) - 1} archive root folder(s))"
            )
        if resolved_base != rel:
            log.info(
                "Resolved DB inside archive at '%s' (matched under a root folder; "
                "requested '%s')", resolved_base, rel
            )

        db_dest = tmpdir / db_name
        _copy_zip_entry(zipf, found["db"], db_dest)

        wal_dest: Optional[Path] = None
        shm_dest: Optional[Path] = None
        if "wal" in found:
            wal_dest = tmpdir / (db_name + WAL_SUFFIX)
            _copy_zip_entry(zipf, found["wal"], wal_dest)
        if "shm" in found:
            shm_dest = tmpdir / (db_name + SHM_SUFFIX)
            _copy_zip_entry(zipf, found["shm"], shm_dest)
        journal_dest: Optional[Path] = None
        if "journal" in found:
            journal_dest = tmpdir / (db_name + JOURNAL_SUFFIX)
            _copy_zip_entry(zipf, found["journal"], journal_dest)

    # STRATEGIC (default): fingerprint only the central directory + the entries
    # we extracted — cheap on huge archives. FULL: whole-file SHA-256 (read
    # twice, before/after) for callers that need a single whole-file digest.
    # NONE: skip integrity capture entirely for metadata-only reads.
    source_hashes: Dict[str, str] = {}
    source_fingerprint: Optional[ZipFingerprint] = None
    if mode == SourceHashMode.FULL:
        source_hashes = snapshot(source_paths)
    elif mode == SourceHashMode.STRATEGIC:
        source_fingerprint = fingerprint_zip(source, found)

    return LocatedDB(
        tmp_db_path=db_dest,
        wal_path=wal_dest,
        shm_path=shm_dest,
        journal_path=journal_dest,
        source_paths=source_paths,
        source_hashes=source_hashes,
        source_fingerprint=source_fingerprint,
    )


def _locate_direct_file(source: Path, tmpdir: Path, mode: SourceHashMode) -> LocatedDB:
    db_name = source.name
    db_dest = tmpdir / db_name

    wal_src = source.with_name(db_name + WAL_SUFFIX)
    shm_src = source.with_name(db_name + SHM_SUFFIX)
    journal_src = source.with_name(db_name + JOURNAL_SUFFIX)

    source_paths: Dict[str, Path] = {"db": source}
    if wal_src.exists():
        source_paths["wal"] = wal_src
    if shm_src.exists():
        source_paths["shm"] = shm_src
    if journal_src.exists():
        source_paths["journal"] = journal_src

    # NONE: read-only preview, no hashing. Otherwise hash before copying.
    source_hashes = {} if mode == SourceHashMode.NONE else snapshot(source_paths)

    wal_dest: Optional[Path] = None
    shm_dest: Optional[Path] = None
    journal_dest: Optional[Path] = None

    shutil.copy2(source, db_dest)
    if wal_src.exists():
        wal_dest = tmpdir / (db_name + WAL_SUFFIX)
        shutil.copy2(wal_src, wal_dest)
    if shm_src.exists():
        shm_dest = tmpdir / (db_name + SHM_SUFFIX)
        shutil.copy2(shm_src, shm_dest)
    if journal_src.exists():
        journal_dest = tmpdir / (db_name + JOURNAL_SUFFIX)
        shutil.copy2(journal_src, journal_dest)

    return LocatedDB(
        tmp_db_path=db_dest,
        wal_path=wal_dest,
        shm_path=shm_dest,
        journal_path=journal_dest,
        source_paths=source_paths,
        source_hashes=source_hashes,
    )


def locate(
    source: Path,
    db_relpath: Optional[Path],
    tmpdir: Path,
    mode: SourceHashMode = SourceHashMode.STRATEGIC,
) -> LocatedDB:
    source = Path(source)
    tmpdir = Path(tmpdir)
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if not tmpdir.is_dir():
        raise NotADirectoryError(f"tmpdir is not a directory: {tmpdir}")

    if is_zip_archive(source):
        if db_relpath is None:
            raise ValueError("db_relpath is required when source is a ZIP archive")
        return _locate_in_zip(source, Path(db_relpath), tmpdir, mode)
    if is_sqlite_database(source):
        # Direct SQLite sources have no archive structure to fingerprint, so
        # STRATEGIC falls back to a full hash; NONE skips hashing.
        return _locate_direct_file(source, tmpdir, mode)

    raise ValueError(
        f"Unsupported source type: {source}. Expected a ZIP archive or SQLite database header."
    )
