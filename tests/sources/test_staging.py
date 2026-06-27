"""Tests for tier-based staging: copy primary-tier sources, read others in place."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from model_atlas.sources.container import FilesystemContainer, SourceFile, acquire_source
from model_atlas.sources.readers.base import get_reader
from model_atlas.sources.staging import (
    STAGING_ALWAYS,
    STAGING_NEVER,
    STAGING_TIER,
    should_copy,
)


def test_should_copy_policy() -> None:
    assert should_copy(STAGING_ALWAYS, "secondary") is True
    assert should_copy(STAGING_NEVER, "primary") is False
    assert should_copy(STAGING_TIER, "primary") is True
    assert should_copy(STAGING_TIER, "secondary") is False
    assert should_copy(STAGING_TIER, None) is False


def _csv_source(tmp_path: Path) -> SourceFile:
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return SourceFile(containers=(FilesystemContainer(tmp_path),), logical_path=PurePosixPath("data.csv"))


def test_acquire_source_in_place_does_not_copy(tmp_path: Path) -> None:
    file = _csv_source(tmp_path)
    staged = acquire_source(file.container, file, copy=False)
    assert staged.temp_dir is None                              # nothing copied
    assert staged.path == tmp_path / "data.csv"                 # the original file
    assert staged.integrity["mode"] == "in_place"

    staged_copy = acquire_source(file.container, file, copy=True)
    assert staged_copy.temp_dir is not None                     # a throwaway copy
    assert staged_copy.path != tmp_path / "data.csv"


def test_csv_reader_honours_tier(tmp_path: Path) -> None:
    file = _csv_source(tmp_path)
    reader = get_reader("csv")
    primary = reader.read(file, {"_tier": "primary"})
    secondary = reader.read(file, {"_tier": "secondary"})
    assert primary.metadata["integrity"]["mode"] == "full"       # copied
    assert secondary.metadata["integrity"]["mode"] == "in_place" # read in place
    # Same data either way.
    assert list(primary.dataframe.columns) == ["a", "b"]
    assert primary.dataframe.equals(secondary.dataframe)
