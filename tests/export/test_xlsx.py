"""Tests for the XLSX export brick (model_atlas/export/xlsx.py).

Round-trips a small store (the golden ``secondary`` fixture) to Excel and
spot-checks row-count/coordinates against the flat CSV (``core.projection.project``)
of the SAME store — the T51 Verify bullet.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from model_atlas.core.projection import CANONICAL_COLUMNS, project
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.xlsx import XlsxExporter
from model_atlas.presets.spec_loader import load_preset_specs

from tests.export._pipeline import GOLDEN_SRC, run_relational_pipeline


def _build_store(store: RunStore) -> None:
    spec = load_preset_specs(GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    run_relational_pipeline(
        store, GOLDEN_SRC / "cached.csv", [spec], linked_entity="subject")


def test_xlsx_round_trips_small_store(tmp_path: Path) -> None:
    with RunStore() as store:
        _build_store(store)
        written = XlsxExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged", merge=True))
        flat = project(store.connection, include_source_columns=False)

    assert written == [tmp_path / "merged.assertions.xlsx"]
    reopened = pd.read_excel(written[0])

    # Row-count spot-check against the flat CSV of the same store.
    assert len(reopened) == len(flat) == 2
    # Column set/order equals the canonical block (no orig_ tail — kml/geojson/xlsx/
    # geovisualizer all project with include_source_columns=False).
    assert list(reopened.columns) == list(CANONICAL_COLUMNS)

    # Coordinate spot-check.
    assert sorted(reopened["latitude_wgs84"].tolist()) == sorted(flat["latitude_wgs84"].tolist())
    assert sorted(reopened["longitude_wgs84"].tolist()) == sorted(flat["longitude_wgs84"].tolist())


def test_xlsx_split_mode_partitions_by_preset(tmp_path: Path) -> None:
    specs = [
        load_preset_specs(GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in ("secondary", "primary")
    ]
    with RunStore() as store:
        run_relational_pipeline(
            store, GOLDEN_SRC / "cached.csv", specs, linked_entity="subject")
        written = XlsxExporter().export(
            store, ExportContext(output_dir=tmp_path, merge=False))

    assert {path.name for path in written} == {
        "golden.secondary.cached.assertions.xlsx", "golden.primary.cached.assertions.xlsx"}
    for path in written:
        assert len(pd.read_excel(path)) == 2
