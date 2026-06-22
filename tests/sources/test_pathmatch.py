"""Tests for anchored, prefix-tolerant path/name matching (sources.pathmatch)."""
from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from model_atlas.sources.pathmatch import name_matches, path_matches, validate_selector_path

_SEL = "/private/var/mobile/Cache.sqlite"


@pytest.mark.parametrize("candidate", [
    "private/var/mobile/Cache.sqlite",                 # bare (skip 0)
    "filesystem1/private/var/mobile/Cache.sqlite",     # wrapper (skip 1)
    "_/private/var/mobile/Cache.sqlite",               # "_" wrapper (skip 1)
])
def test_prefix_tolerance_depth_1_matches(candidate: str) -> None:
    assert path_matches(_SEL, PurePosixPath(candidate), root_prefix_depth=1)


def test_two_segment_prefix_does_not_match_at_depth_1() -> None:
    candidate = PurePosixPath("dump/filesystem1/private/var/mobile/Cache.sqlite")
    assert not path_matches(_SEL, candidate, root_prefix_depth=1)
    assert path_matches(_SEL, candidate, root_prefix_depth=2)


def test_uuid_segment_matches_real_uuid_only() -> None:
    sel = "/private/{uuid}/Cache.sqlite"
    ok = PurePosixPath("private/00008110-001949C40244801E-AABBCCDD-EEFF-0011/Cache.sqlite")
    real = PurePosixPath("private/12345678-1234-1234-1234-123456789abc/Cache.sqlite")
    bad = PurePosixPath("private/not-a-uuid/Cache.sqlite")
    assert path_matches(sel, real, root_prefix_depth=0)
    assert not path_matches(sel, bad, root_prefix_depth=0)
    assert not path_matches(sel, ok, root_prefix_depth=0)  # wrong segment shape


def test_star_matches_exactly_one_segment() -> None:
    sel = "/a/*/c"
    assert path_matches(sel, PurePosixPath("a/b/c"), root_prefix_depth=0)
    assert not path_matches(sel, PurePosixPath("a/b/x/c"), root_prefix_depth=0)


def test_segment_count_must_match() -> None:
    assert not path_matches(_SEL, PurePosixPath("private/var/Cache.sqlite"), root_prefix_depth=1)


def test_name_matches_is_exact_basename() -> None:
    assert name_matches("Cache.sqlite", PurePosixPath("a/b/Cache.sqlite"))
    assert not name_matches("Cache.sqlite", PurePosixPath("a/b/Other.sqlite"))


def test_double_star_rejected() -> None:
    with pytest.raises(ValueError):
        validate_selector_path("/a/**/c")


def test_bad_placeholder_rejected() -> None:
    with pytest.raises(ValueError):
        validate_selector_path("/a/{id}/c")
