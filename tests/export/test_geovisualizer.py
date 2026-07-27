"""Tests for the GeoVisualizer export brick (model_atlas/export/geovisualizer.py).

Two stores exercise this brick:
  - the golden ``secondary`` fixture (both assertions instant): round-trip a small
    store, spot-check row-count/coordinates against the flat CSV of the SAME store;
  - the hand-authored manual store (tests/export/_pipeline.build_manual_store), whose
    second assertion is a genuine interval with a compound IMEI-bearing identifier —
    the only way to exercise the interval-bound expansion and the IMEI port, since the
    golden fixtures are instant-only, IMEI-free.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from model_atlas.core.flat_view import build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.geovisualizer import GeovisualizerExporter, _GEOVIS_COLUMNS
from model_atlas.presets.spec_loader import load_preset_specs

from tests.export._pipeline import GOLDEN_SRC, MANUAL_IMEI, build_manual_store, run_relational_pipeline


def _read_geovis_csv(path: Path) -> pd.DataFrame:
    # dtype=str for IMEI: QUOTE_MINIMAL (matching the old exporter) leaves a bare
    # digit string unquoted, so a generic reader would otherwise infer float64 and
    # round the 15-digit IMEI — a read-back artefact of this test, not the file on
    # disk (the bytes on disk are the exact digit string; see test_sqlite_db-style
    # byte-level checks in the other export tests for the written-bytes assertion).
    return pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype={"IMEI": str})


def _build_golden_store(store: RunStore) -> None:
    spec = load_preset_specs(GOLDEN_SRC / "secondary_cached.preset.yaml")[0]
    run_relational_pipeline(
        store, GOLDEN_SRC / "cached.csv", [spec], linked_entity="subject")


# --- golden round-trip: row-count/coordinate spot-check against the flat CSV --------

def test_geovisualizer_round_trips_small_store(tmp_path: Path) -> None:
    with RunStore() as store:
        _build_golden_store(store)
        written = GeovisualizerExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged"))
        flat = build_flat_view(store.connection, include_source_columns=False)

    assert written == [tmp_path / "merged.geovisualizer.csv"]
    out = _read_geovis_csv(written[0])

    # Both golden assertions are instants (lower == upper) -> no expansion, one
    # GeoVisualizer row per assertion.
    assert list(out.columns) == list(_GEOVIS_COLUMNS)
    assert len(out) == len(flat) == 2
    assert sorted(out["Latitude"].tolist()) == sorted(flat["latitude_wgs84"].tolist())
    assert sorted(out["Longitude"].tolist()) == sorted(flat["longitude_wgs84"].tolist())
    assert (out["Type"] == "iOS AXIOM — Golden Secondary Cached").all()
    assert sorted(out["Precision"].tolist()) == sorted(flat["horizontal_accuracy_m"].tolist())


# --- manual store: interval expansion + IMEI extraction -----------------------------

def test_geovisualizer_expands_interval_into_two_rows(tmp_path: Path) -> None:
    with RunStore() as store:
        build_manual_store(store)
        written = GeovisualizerExporter().export(
            store, ExportContext(output_dir=tmp_path, base_name="merged"))
        flat = build_flat_view(store.connection, include_source_columns=False)

    out = _read_geovis_csv(written[0])
    # 2 assertions in the store: the instant one yields 1 row, the interval one yields
    # 2 (one per bound) -> 3 GeoVisualizer rows total.
    assert len(flat) == 2
    assert len(out) == 3

    interval_rows = out[out["IMEI"] == MANUAL_IMEI]
    assert len(interval_rows) == 2
    # Both bound-rows share position/IMEI/target (only the timestamp/description differ).
    assert interval_rows["Latitude"].nunique() == 1
    assert interval_rows["Longitude"].nunique() == 1
    assert set(interval_rows["Target"]) == {"subject"}

    descriptions = " ".join(interval_rows["Description"].tolist())
    assert "Range start" in descriptions
    assert "Range end" in descriptions
    # The upper bound's offset was written as UTC_OFFSET_UNKNOWN -> the warning fires
    # on exactly that row, not the lower one.
    assert (interval_rows["Description"].str.contains("UTC offset unknown")).sum() == 1

    # The two bound timestamps differ (13:00 vs 14:30 local).
    assert interval_rows["Timestamp (local)"].nunique() == 2

    # The instant assertion's row carries no IMEI (identifier is None) and a concrete
    # speed (10.8 km/h, not "null").
    instant_row = out[out["IMEI"] != MANUAL_IMEI].iloc[0]
    assert instant_row["IMEI"] == "" or pd.isna(instant_row["IMEI"])
    assert "Speed: 10.8 km/h" in instant_row["Description"]


def test_geovisualizer_imei_extraction_is_luhn_checked() -> None:
    from model_atlas.export.geovisualizer import _pick_imei

    # The valid, Luhn-checked IMEI embedded in a compound identifier is extracted...
    assert _pick_imei("dev_490154203237518-x") == "490154203237518"
    # ...but a same-length, Luhn-INVALID token is rejected (never returned as an IMEI).
    assert _pick_imei("123456789012345") == ""
    assert _pick_imei(None) == ""
    assert _pick_imei("") == ""
