"""Tests for the new-engine export registry (model_atlas/export/registry.py).

Importing the five T51 brick modules registers each at import time; this checks the
mechanism itself (listing, duplicate-name rejection, run_exporters' resilience) rather
than any one brick's serialisation (covered in its own test module).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import model_atlas.export.geojson  # noqa: F401 - import for registration side effect
import model_atlas.export.geovisualizer  # noqa: F401
import model_atlas.export.kml  # noqa: F401
import model_atlas.export.sqlite_db  # noqa: F401
import model_atlas.export.xlsx  # noqa: F401
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import available_exporters, get_exporter, register_exporter, run_exporters

from tests.export._pipeline import build_manual_store


def test_all_t51_bricks_are_registered() -> None:
    names = {spec.name for spec in available_exporters()}
    assert {"kml", "geojson", "xlsx", "geovisualizer", "sqlite_db"} <= names
    for name in ("kml", "geojson", "xlsx", "geovisualizer", "sqlite_db"):
        spec = get_exporter(name)
        assert spec is not None
        assert spec.exporter.format == name


def test_duplicate_name_registration_raises() -> None:
    spec = get_exporter("kml")
    with pytest.raises(ValueError, match="already registered"):
        register_exporter("kml", "duplicate", spec.exporter)


def test_run_exporters_writes_every_selected_format_and_skips_unknown(tmp_path: Path) -> None:
    with RunStore() as store:
        build_manual_store(store)
        ctx = ExportContext(output_dir=tmp_path, base_name="merged", merge=True)
        written = run_exporters(["kml", "geojson", "not_a_real_format"], store, ctx)

    assert {path.suffix for path in written} == {".kml", ".geojson"}
    assert len(written) == 2
