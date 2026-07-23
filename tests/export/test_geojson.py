"""Tests for the GeoJSON export brick (model_atlas/export/geojson.py).

Round-trips a small store (the golden ``secondary`` fixture) to GeoJSON and
spot-checks feature count and coordinates against the flat CSV
(``core.projection.project``) of the SAME store — the T51 Verify bullet. Also checks
the encoding decision (plain utf-8, NO BOM — RFC 7946 §5, a deliberate deviation from
the old exporter, see model_atlas/export/geojson.py's docstring).
"""
from __future__ import annotations

import json
from pathlib import Path

from model_atlas.core.projection import project
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.geojson import GeojsonExporter
from model_atlas.presets.spec_loader import load_preset_specs

from tests.export._pipeline import GOLDEN_SRC, run_relational_pipeline


def _build_store(store: RunStore) -> None:
    spec = load_preset_specs(GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    run_relational_pipeline(
        store, GOLDEN_SRC / "cached.csv", [spec], linked_entity="subject")


def test_geojson_round_trips_small_store(tmp_path: Path) -> None:
    with RunStore() as store:
        _build_store(store)
        written = GeojsonExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged"))
        flat = project(store.connection, include_source_columns=False)

    assert written == [tmp_path / "merged.points.geojson"]
    data = written[0].read_bytes()

    # RFC 7946 §5: no BOM — a documented deviation from the old exporter's utf-8-sig.
    assert not data.startswith(b"\xef\xbb\xbf")

    payload = json.loads(data.decode("utf-8"))
    assert payload["type"] == "FeatureCollection"
    features = payload["features"]

    located = flat.dropna(subset=["latitude_wgs84", "longitude_wgs84"])
    assert len(features) == len(located) == 2

    expected_coords = {
        (row.longitude_wgs84, row.latitude_wgs84) for row in located.itertuples()
    }
    actual_coords = {tuple(feature["geometry"]["coordinates"]) for feature in features}
    assert actual_coords == expected_coords

    for feature in features:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Point"
        properties = feature["properties"]
        # lat/lon do not repeat inside properties (they are the geometry).
        assert "latitude_wgs84" not in properties
        assert "longitude_wgs84" not in properties
        assert properties["entity_type"] == "device"
        assert properties["linked_entity"] == "subject"


def test_geojson_writes_one_file_for_all_presets(tmp_path: Path) -> None:
    # One file per format: two presets applied to one source land in a single GeoJSON.
    specs = [
        load_preset_specs(GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in ("secondary", "primary")
    ]
    with RunStore() as store:
        run_relational_pipeline(
            store, GOLDEN_SRC / "cached.csv", specs, linked_entity="subject")
        written = GeojsonExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="case"))

    assert [path.name for path in written] == ["case.points.geojson"]
    payload = json.loads(written[0].read_bytes().decode("utf-8"))
    assert len(payload["features"]) == 4      # both presets' rows (2 each) in the one file
