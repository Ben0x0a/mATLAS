"""Tests for the ingest engine's ported path/name matcher (ingest/pathmatch.py).

The ingest engine owns its own copy of the selector path model (the old
the old ``sources/pathmatch.py`` retired); these pin the ported behaviour —
segment-wise anchored matching, the ``{uuid}``/``*`` wildcards, the root-prefix skip
budget, and the ``**`` rejection.
"""
from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from model_atlas.ingest.pathmatch import name_matches, path_matches, validate_selector_path


def test_full_anchored_match_with_root_prefix_skip() -> None:
    candidate = PurePosixPath("filesystem1/private/var/mobile/sms.db")
    # the wrapper root segment is skipped by the default root_prefix_depth=1
    assert path_matches("private/var/mobile/sms.db", candidate)
    # a suffix search is NOT allowed: unequal segment counts never match at skip 0/1
    assert not path_matches("mobile/sms.db", candidate)


def test_uuid_and_star_wildcards() -> None:
    uuid_seg = "12345678-1234-1234-1234-1234567890ab"
    candidate = PurePosixPath(f"{uuid_seg}/Media/photo.jpg")
    assert path_matches("{uuid}/Media/photo.jpg", candidate, root_prefix_depth=0)
    assert path_matches("{uuid}/*/photo.jpg", candidate, root_prefix_depth=0)
    assert not path_matches("{uuid}/Media/other.jpg", candidate, root_prefix_depth=0)


def test_double_star_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_selector_path("private/**/sms.db")


def test_name_matches_is_exact_basename() -> None:
    assert name_matches("sms.db", PurePosixPath("a/b/sms.db"))
    assert not name_matches("sms.db", PurePosixPath("a/b/sms.db.bak"))
