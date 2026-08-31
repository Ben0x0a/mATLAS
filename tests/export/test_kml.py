"""Tests for the KML export brick (model_atlas/export/kml.py).

Round-trips a small store (the golden ``secondary`` fixture) to KML and spot-checks
placemark count and coordinates against the flat CSV (``core.flat_view.build_flat_view``) of
the SAME store. Also checks the encoding decision (plain
utf-8, no BOM — required for a valid KML/XML declaration) and split-mode partitioning.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from model_atlas.core.flat_view import build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.kml import KmlExporter

from tests.export._pipeline import GOLDEN_SRC, run_relational_pipeline
from model_atlas.presets.spec_loader import load_preset_specs

_KML_NS = "{http://www.opengis.net/kml/2.2}"


def _build_store(store: RunStore) -> None:
    spec = load_preset_specs(GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    run_relational_pipeline(
        store, GOLDEN_SRC / "cached.csv", [spec], concerns="subject")


def test_kml_round_trips_small_store(tmp_path: Path) -> None:
    with RunStore() as store:
        _build_store(store)
        written = KmlExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged"))
        flat = build_flat_view(store.connection, include_source_columns=False)

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

    # name = concerns ("subject" for both rows in this fixture).
    names = {placemark.find(f"{_KML_NS}name").text for placemark in placemarks}
    assert names == {"subject"}

    # A TimeStamp/when is present (an instant observation — lower bound is set).
    for placemark in placemarks:
        when = placemark.find(f"{_KML_NS}TimeStamp/{_KML_NS}when")
        assert when is not None and when.text


def test_kml_writes_one_file_for_all_presets(tmp_path: Path) -> None:
    # One file per format: two presets applied to one source land in a single KML.
    specs = [
        load_preset_specs(GOLDEN_SRC / f"{case}_cached.preset.yaml")[0]
        for case in ("secondary", "primary")
    ]
    with RunStore() as store:
        run_relational_pipeline(
            store, GOLDEN_SRC / "cached.csv", specs, concerns="subject")
        written = KmlExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="case"))

    assert [path.name for path in written] == ["case.points.kml"]
    placemarks = ET.fromstring(written[0].read_bytes()).findall(f".//{_KML_NS}Placemark")
    assert len(placemarks) == 4       # both presets' rows (2 each) in the one file
