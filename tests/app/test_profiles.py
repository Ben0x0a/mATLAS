"""Tests for the CLI/GUI profile helpers (model_atlas.app.profiles).

Focuses on the filename-suffix handling: a dotted profile name must keep every segment
(save_profile builds the name by concatenation, not with_suffix, which would treat the last
dotted segment as an extension and drop it).
Used by:    pytest.
Depends on: model_atlas.app.profiles only.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.app.profiles import PROFILE_SUFFIX, load_profile, save_profile


def test_dotted_profile_name_keeps_every_segment(tmp_path: Path) -> None:
    # "operation.falcon" must round-trip to "operation.falcon.mATLAS-profile" — with_suffix
    # would have mangled it to "operation.mATLAS-profile", losing the "falcon" segment.
    preset = tmp_path / "p.yaml"
    preset.write_text("selectors: []\n", encoding="utf-8")

    written = save_profile(tmp_path / "operation.falcon", [preset])

    assert written.name == f"operation.falcon{PROFILE_SUFFIX}"
    assert written.is_file()
    # And it loads back to the same preset (a genuine round-trip, not just a name check).
    assert load_profile(written) == [preset.resolve()]


def test_existing_suffix_is_not_doubled(tmp_path: Path) -> None:
    # A name already carrying the full suffix is left as-is (no "…mATLAS-profile.mATLAS-profile").
    preset = tmp_path / "p.yaml"
    preset.write_text("selectors: []\n", encoding="utf-8")

    written = save_profile(tmp_path / f"case{PROFILE_SUFFIX}", [preset])

    assert written.name == f"case{PROFILE_SUFFIX}"
