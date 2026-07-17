"""Tests for the KML export brick (model_atlas/export/kml.py).

Round-trips a small store (the golden ``secondary`` fixture) to KML and spot-checks
placemark count and coordinates against the flat CSV (``core.projection.project``) of
the SAME store — the T51 Verify bullet. Also checks the encoding decision (plain
utf-8, no BOM — required for a valid KML/XML declaration) and split-mode partitioning.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from model_atlas.core.projection import project
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.kml import KmlExporter

from tests.export._pipeline import GOLDEN_SRC, run_relational_pipeline
from model_atlas.presets.spec_loader import load_preset_specs

_KML_NS = "{http://www.opengis.net/kml/2.2}"


def _build_store(store: RunStore) -> None:
    spec = load_preset_specs(GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    run_relational_pipeline(
        store, GOLDEN_SRC / "cached.csv", [spec], linked_entity="subject")


def test_kml_round_trips_small_store(tmp_path: Path) -> None:
    with RunStore() as store:
        _build_store(store)
        written = KmlExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged", merge=True))
        flat = project(store.connection, include_source_columns=False)

    assert written == [tmp_path / "merged.points.kml"]
    data = written[0].read_bytes()

    # Encoding: plain utf-8, no BOM (an invalid XML declaration otherwise).
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.startswith(b"<?xml version='1.0' encoding='utf-8'?>")

    root = ET.fromstring(data)
    placemarks = root.findall(f".//{_KML_NS}Placemark")
    # Row-count spot-check: every projected row carries a coordinate in this fixture.
    located = flat.dropna(subset=["latitude_wgs84", "longitude_wgs84"])
    assert len(placemarks) == len(located) == 2

    # Coordinate spot-check: "lon,lat" against the SAME rows' flat-CSV coordinates.
    expected_coords = {
        f"{row.longitude_wgs84},{row.latitude_wgs84}" for row in located.itertuples()
    }
    actual_coords = {
        placemark.find(f"{_KML_NS}Point/{_KML_NS}coordinates").text
        for placemark in placemarks
    }
    assert actual_coords == expected_coords

    # name = linked_entity ("subject" for both rows in this fixture).
    names = {placemark.find(f"{_KML_NS}name").text for placemark in placemarks}
    assert names == {"subject"}

    # A TimeStamp/when is present (an instant assertion — lower bound is set).
    for placemark in placemarks:
        when = placemark.find(f"{_KML_NS}TimeStamp/{_KML_NS}when")
        assert when is not None and when.text


def test_kml_split_mode_partitions_by_preset(tmp_path: Path) -> None:
    specs = [
        load_preset_specs(GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in ("secondary", "primary")
    ]
    with RunStore() as store:
        run_relational_pipeline(
            store, GOLDEN_SRC / "cached.csv", specs, linked_entity="subject")
        written = KmlExporter().export(
            store, ExportContext(output_dir=tmp_path, merge=False))

    assert {path.name for path in written} == {
        "golden.secondary.cached.points.kml", "golden.primary.cached.points.kml"}
    for path in written:
        placemarks = ET.fromstring(path.read_bytes()).findall(f".//{_KML_NS}Placemark")
        assert len(placemarks) == 2
