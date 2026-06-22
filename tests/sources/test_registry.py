"""Tests for the format-reader registry dispatch.

Defines:    tests that the built-in readers register and dispatch by format string.
Used by:    pytest.
Depends on: model_atlas.sources.readers (importing it registers the readers).
"""
from __future__ import annotations

import pytest

from model_atlas.sources.readers import get_reader, registered_readers


def test_builtin_readers_are_registered() -> None:
    formats = {reader.format for reader in registered_readers()}
    assert {"csv", "excel", "sqlite"} <= formats


def test_dispatch_by_format() -> None:
    assert get_reader("csv").format == "csv"
    assert get_reader("excel").format == "excel"
    assert get_reader("sqlite").format == "sqlite"


def test_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="no reader for format"):
        get_reader("parquet")
