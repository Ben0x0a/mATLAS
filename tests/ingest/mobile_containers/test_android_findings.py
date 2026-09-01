"""The Android resolver reports its oddities instead of dropping them.

Defines:    tests for the three conditions ``AndroidResolver.findings`` records.
Used by:    the test suite.
Depends on: the public surface of ``mobile_containers`` and the standard library.

WHY these exist: ``findings`` was iOS-only, so on Android an acquisition with no data tree,
a malformed user segment, or one package resolving to two paths of the same kind was
indistinguishable from a clean run that simply found nothing. Each is now recorded under
the same tag the iOS side uses where the condition is shared.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from model_atlas.ingest.utils.mobile_containers import (
    AndroidResolver,
    ZipOpener,
)
from model_atlas.ingest.utils.mobile_containers.categories import (
    AMBIGUOUS_IDENTIFIER_FINDING_NAME,
    MISSING_DATA_TREE_FINDING_NAME,
    UNPARSABLE_USER_ID_FINDING_NAME,
)


def _archive(tmp_path: Path, entries: tuple[str, ...], name: str) -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for entry in entries:
            zf.writestr(entry, b"x")
    return archive


def _findings(archive: Path) -> tuple[str, ...]:
    with ZipOpener(archive) as opener:
        return tuple(f.kind for f in AndroidResolver(opener).findings)


def test_no_data_tree_is_recorded_not_merely_logged(tmp_path: Path) -> None:
    """'Wrong acquisition' must be distinguishable from 'this app owns nothing'."""
    archive = _archive(tmp_path, ("Dump/apex/com.android.art/etc/boot.art",), "no_tree.zip")
    assert MISSING_DATA_TREE_FINDING_NAME in _findings(archive)


def test_a_non_numeric_user_segment_is_recorded(tmp_path: Path) -> None:
    """A layout we claim to understand, shaped unexpectedly, is worth reporting."""
    archive = _archive(tmp_path, (
        "Dump/data/data/com.example.app/files/a.db",          # anchors the root depth
        "Dump/data/user/notanumber/com.example.app/files/b",  # the odd one
    ), "bad_user.zip")
    assert UNPARSABLE_USER_ID_FINDING_NAME in _findings(archive)


def test_one_package_two_paths_of_one_kind_is_reported(tmp_path: Path) -> None:
    """Same condition, same tag as the iOS side — never a silent first-wins."""
    archive = _archive(tmp_path, (
        "Dump/data/media/0/Android/data/com.example.app/files/a",
        "Dump/data/media/0/Android/media/com.example.app/files/b",
        "Dump/data/user_de/0/com.example.app/files/c",
        "Dump/data/user_de/00/com.example.app/files/d",   # user 0 written two ways
    ), "ambiguous.zip")
    assert AMBIGUOUS_IDENTIFIER_FINDING_NAME in _findings(archive)


def test_a_clean_acquisition_reports_nothing(tmp_path: Path) -> None:
    """Findings must mean something: a well-formed tree produces none."""
    archive = _archive(tmp_path, (
        "Dump/data/data/com.example.app/files/a.db",
        "Dump/data/user_de/0/com.example.app/files/b.db",
    ), "clean.zip")
    assert _findings(archive) == ()
