"""Tests for the source-adapter registry dispatch.

Defines:    tests that the built-in adapters register and dispatch by element type.
Used by:    pytest.
Depends on: model_atlas.sources (importing it registers the adapters).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.sources import get_adapter, registered_adapters


def _element(source_type: ElementType) -> DiscoveredElement:
    return DiscoveredElement(
        source_type=source_type,
        path=Path("/tmp/example"),
        source_file="example",
        source_original_path="/tmp/example",
        logical_name="example",
    )


def test_builtin_adapters_are_registered() -> None:
    names = {adapter.name for adapter in registered_adapters()}
    assert {"csv", "excel", "sqlite"} <= names


def test_dispatch_by_element_type() -> None:
    assert get_adapter(_element(ElementType.CSV)).name == "csv"
    assert get_adapter(_element(ElementType.EXCEL)).name == "excel"
    assert get_adapter(_element(ElementType.SQLITE)).name == "sqlite"


def test_registration_is_idempotent() -> None:
    import importlib

    import model_atlas.sources.csv_source as csv_source

    before = len(registered_adapters())
    importlib.reload(csv_source)  # re-running @register_adapter must not duplicate
    assert len(registered_adapters()) == before


def test_unknown_element_raises() -> None:
    class _Fake:
        source_type = type("T", (), {"value": "parquet"})()

        def __getattr__(self, name: str) -> object:
            return None

    with pytest.raises(ValueError):
        get_adapter(_Fake())  # type: ignore[arg-type]
