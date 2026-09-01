"""A container proven only by directory entries must still be found.

Defines:    the regression test for the opener dropping directory entries.
Used by:    the test suite.
Depends on: the public surface of ``model_atlas.ingest.utils.mobile_containers`` and the
            standard library only.

WHY this test exists: ``ZipOpener.list_paths`` originally skipped directory entries on the
reasoning that "consumers derive directories from file paths". That reasoning fails for a
container whose contents are entirely subdirectories — there is no file to derive it from,
so the container becomes invisible and ``resolve`` silently returns fewer locations than
exist. Measured on the real Pixel acquisition before the fix:
``data/media/0/Android/data/com.whatsapp`` (six directory entries, no file) was missed, so
WhatsApp resolved to 3 of its 4 locations, and every location of
``com.android.backupconfirm`` is a bare directory, so it resolved to 0 of 4.

Silently returning a subset is the one thing this library must never do, so the case is
pinned here rather than left to the real-acquisition check.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from model_atlas.ingest.utils.mobile_containers import (
    ANDROID_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
    AndroidResolver,
    DirectoryOpener,
    ZipOpener,
)

_PKG = "com.example.dirsonly"
_ROOT = "Dump"
# Every entry below is a DIRECTORY. The archive contains no file for this package at all,
# which is the exact shape that used to vanish.
_DIRECTORY_ONLY_ENTRIES = (
    f"{_ROOT}/data/data/{_PKG}/",
    f"{_ROOT}/data/data/{_PKG}/files/",
    f"{_ROOT}/data/data/{_PKG}/cache/",
    f"{_ROOT}/data/media/0/Android/data/{_PKG}/",
    f"{_ROOT}/data/media/0/Android/data/{_PKG}/files/",
)
# One unrelated FILE, so the acquisition still has a data tree to detect the root depth
# from in the way a real one would.
_ANCHOR_FILE = f"{_ROOT}/data/data/com.example.other/databases/main.db"


def _archive(tmp_path: Path) -> Path:
    archive = tmp_path / "directory_only.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for entry in _DIRECTORY_ONLY_ENTRIES:
            zf.writestr(entry, b"")
        zf.writestr(_ANCHOR_FILE, b"x")
    return archive


def _folder(tmp_path: Path) -> Path:
    root = tmp_path / "extracted"
    for entry in _DIRECTORY_ONLY_ENTRIES:
        (root / entry).mkdir(parents=True, exist_ok=True)
    anchor = root / _ANCHOR_FILE
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"x")
    return root


def test_zip_opener_yields_directory_entries(tmp_path: Path) -> None:
    """The opener must surface directories; deriving them from files loses containers."""
    with ZipOpener(_archive(tmp_path)) as opener:
        paths = set(opener.list_paths())
    assert f"{_ROOT}/data/media/0/Android/data/{_PKG}" in paths, (
        "a directory entry was dropped — a container with no file inside it becomes "
        "invisible, which is a silent loss"
    )


def test_container_with_only_directories_is_resolved_from_a_zip(tmp_path: Path) -> None:
    with ZipOpener(_archive(tmp_path)) as opener:
        containers = AndroidResolver(opener).resolve(_PKG)
    categories = {c.category for c in containers}
    assert categories == {ANDROID_DATA_CATEGORY_NAME, ANDROID_EXTERNAL_DATA_CATEGORY_NAME}, (
        f"expected both directory-only containers, got {sorted(categories)}"
    )


def test_container_with_only_directories_is_resolved_from_a_folder(tmp_path: Path) -> None:
    """The two openers must agree: a folder acquisition answers like the archive."""
    containers = AndroidResolver(DirectoryOpener(_folder(tmp_path))).resolve(_PKG)
    categories = {c.category for c in containers}
    assert categories == {ANDROID_DATA_CATEGORY_NAME, ANDROID_EXTERNAL_DATA_CATEGORY_NAME}, (
        f"folder opener disagreed with the zip opener: {sorted(categories)}"
    )
