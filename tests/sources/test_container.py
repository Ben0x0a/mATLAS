"""Unit tests for the Container layer: staging, kind-aware integrity, sibling
collection, on-disk path resolution, and the SourceFile provenance properties."""
from __future__ import annotations

import hashlib
import os
import pytest
import zipfile
from pathlib import Path, PurePosixPath

from model_atlas.integrity import sha256_file
from model_atlas.sources.container import (
    FilesystemContainer,
    SourceFile,
    ZipBombError,
    ZipContainer,
)


def _fs_file(root: Path, rel: str) -> tuple[FilesystemContainer, SourceFile]:
    container = FilesystemContainer(root)
    return container, SourceFile(containers=(container,), logical_path=PurePosixPath(rel))


# --- filesystem staging + integrity ---------------------------------------

def test_filesystem_stage_copies_and_hashes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    container, file = _fs_file(root, "a.txt")

    staged = container.stage(file)

    assert staged.path != root / "a.txt"                   # a copy, not the original
    assert staged.path.read_text(encoding="utf-8") == "hello"
    assert staged.fingerprint == sha256_file(root / "a.txt")
    assert staged.integrity["mode"] == "full"
    assert staged.integrity["ok"] is None                  # not verified until finalize

    container.finalize(staged)
    assert staged.integrity["ok"] is True                  # original unchanged after read


def test_filesystem_finalize_detects_a_modified_original(tmp_path: Path) -> None:
    """The before/after hash must actually catch a tampered original — otherwise the
    integrity ``ok`` flag is worthless."""
    root = tmp_path / "root"
    root.mkdir()
    original = root / "a.txt"
    original.write_text("evidence", encoding="utf-8")
    container, file = _fs_file(root, "a.txt")

    staged = container.stage(file)
    original.write_text("TAMPERED", encoding="utf-8")      # mutated during the "read"
    container.finalize(staged)

    assert staged.integrity["ok"] is False


def test_filesystem_stage_group_collects_only_existing_siblings(tmp_path: Path) -> None:
    d = tmp_path / "root" / "d"
    d.mkdir(parents=True)
    db = d / "x.sqlite"
    db.write_bytes(b"SQLite format 3\x00body")
    (d / "x.sqlite-wal").write_bytes(b"wal-bytes")
    (d / "x.sqlite-shm").write_bytes(b"shm-bytes")
    # no -journal on disk
    container, file = _fs_file(tmp_path / "root", "d/x.sqlite")

    group = container.stage_group(file)

    assert set(group.members) == {"db", "wal", "shm"}      # journal absent -> omitted
    assert group.fingerprint == sha256_file(db)
    container.finalize(group)
    assert group.integrity["ok"] is True


def test_filesystem_ondisk_path_is_the_real_file(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "a").write_text("x", encoding="utf-8")
    container, file = _fs_file(root, "a")
    assert container.ondisk_path(file) == root / "a"


# --- zip staging ----------------------------------------------------------

def test_zip_stage_extracts_and_content_hashes(tmp_path: Path) -> None:
    payload = b"some-bytes-1234"
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("dir/file.bin", payload)
    container = ZipContainer(path=archive)
    file = next(f for f in container.files() if f.name == "file.bin")

    staged = container.stage(file)

    assert staged.path.read_bytes() == payload
    assert staged.fingerprint == hashlib.sha256(payload).hexdigest()
    assert staged.integrity["mode"] == "strategic"


def test_zip_stage_group_from_bytes_hashes_member(tmp_path: Path) -> None:
    raw = tmp_path / "inner.zip"
    with zipfile.ZipFile(raw, "w") as z:
        z.writestr("p/x.sqlite", b"SQLite format 3\x00X")
    container = ZipContainer(data=raw.read_bytes(), label="inner.zip")  # nested: no path
    file = next(f for f in container.files() if f.name == "x.sqlite")

    group = container.stage_group(file)

    assert "db" in group.members
    assert group.fingerprint == sha256_file(group.members["db"])
    assert group.integrity["mode"] == "strategic"


def test_zip_ondisk_path_is_none(tmp_path: Path) -> None:
    archive = tmp_path / "z.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("a", b"x")
    container = ZipContainer(path=archive)
    file = next(iter(container.files()))
    assert container.ondisk_path(file) is None             # an entry has no independent path


# --- SourceFile provenance properties -------------------------------------

def test_container_chain_lists_only_archives(tmp_path: Path) -> None:
    fs = FilesystemContainer(tmp_path)
    zc = ZipContainer(path=tmp_path / "E.zip")             # not opened; label from name
    nested = SourceFile(
        containers=(fs, zc),
        logical_path=PurePosixPath("filesystem1/private/db.sqlite"),
    )
    assert nested.container_chain == ["E.zip"]             # folder excluded, archive listed
    assert str(nested.full_logical_path) == "E.zip/filesystem1/private/db.sqlite"

    loose = SourceFile(containers=(fs,), logical_path=PurePosixPath("a/b.csv"))
    assert loose.container_chain == []
    assert loose.name == "b.csv"


# --- zip stage_group strategic integrity ----------------------------------

def _zip_with_db_and_wal(archive: Path) -> None:
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("p/x.sqlite", b"SQLite format 3\x00body")
        z.writestr("p/x.sqlite-wal", b"wal-bytes")


def test_zip_stage_group_integrity_ok_when_archive_unchanged(tmp_path: Path) -> None:
    archive = tmp_path / "a.zip"
    _zip_with_db_and_wal(archive)
    container = ZipContainer(path=archive)
    file = next(f for f in container.files() if f.name == "x.sqlite")

    group = container.stage_group(file)
    container.finalize(group)

    assert set(group.members) == {"db", "wal"}
    assert group.integrity["mode"] == "strategic"
    assert group.integrity["ok"] is True


def test_zip_stage_group_integrity_detects_archive_tampering(tmp_path: Path) -> None:
    """The strategic fingerprint is captured at stage time; rewriting the archive before
    finalize must be caught (ok False) — proving the zip integrity check has teeth, like
    the filesystem before/after hash."""
    archive = tmp_path / "a.zip"
    _zip_with_db_and_wal(archive)
    container = ZipContainer(path=archive)
    file = next(f for f in container.files() if f.name == "x.sqlite")

    group = container.stage_group(file)
    with zipfile.ZipFile(archive, "w") as z:               # mutate the archive mid-run
        z.writestr("totally/different.txt", b"changed")
    container.finalize(group)

    assert group.integrity["ok"] is False


# --- zip-bomb decompression guard -----------------------------------------

def test_zip_bomb_entry_is_refused(tmp_path: Path) -> None:
    """A highly compressible entry that blows past the ratio cap is refused, not extracted."""
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.bin", b"\x00" * (4 * 1024 * 1024))   # 4 MiB of zeros -> tiny compressed
    container = ZipContainer(path=archive)
    container.min_ratio_check_bytes = 256 * 1024             # lower the floor for a fast test
    container.max_decompression_ratio = 10
    file = next(f for f in container.files() if f.name == "big.bin")

    with pytest.raises(ZipBombError):
        container.stage(file)


def test_modestly_compressed_large_entry_is_allowed(tmp_path: Path) -> None:
    """Incompressible (stored) data of the same size passes — only the *ratio* trips."""
    archive = tmp_path / "ok.zip"
    payload = os.urandom(2 * 1024 * 1024)                    # random -> ~1:1, not a bomb
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("data.bin", payload)
    container = ZipContainer(path=archive)
    container.min_ratio_check_bytes = 256 * 1024
    container.max_decompression_ratio = 10
    file = next(f for f in container.files() if f.name == "data.bin")

    staged = container.stage(file)                           # no raise
    assert staged.path.stat().st_size == len(payload)


def test_small_high_ratio_entry_is_allowed(tmp_path: Path) -> None:
    """A tiny entry below the floor passes regardless of ratio (the floor avoids
    false positives on small, legitimately compressible files)."""
    archive = tmp_path / "small.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("note.txt", b"a" * 50_000)                # high ratio but small output
    container = ZipContainer(path=archive)                   # default 64 MiB floor
    file = next(f for f in container.files() if f.name == "note.txt")

    staged = container.stage(file)                           # below floor -> allowed
    assert staged.path.stat().st_size == 50_000
