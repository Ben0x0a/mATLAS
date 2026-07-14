"""Tests for the additional-export subsystem (model_atlas.exporters).

Covers the registry/runner contract, drop-in plugin discovery, the five built-in exporters, the
GeoVisualizer column mapping, and end-to-end wiring through the pipeline.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from model_atlas.exporters import (
    ExportContext,
    available_exporters,
    get_exporter,
    load_plugins,
    register_exporter,
    run_exporters,
)
from model_atlas.exporters import builtin as _builtin
from model_atlas.exporters import registry as _registry
from model_atlas.model.families import Entity, SpatioTemporalAssertion
from model_atlas.model.store import AssertionRow, RecordRow, RunStore, SourceRow


def _ctx(
    dataframe: pd.DataFrame, output_dir: Path, connection=None
) -> ExportContext:
    # The ``sqlite`` export now backs up ctx.connection (the run-wide store), not the frame, so
    # every context carries a store connection; unless a populated store is supplied, an empty one
    # is used (a valid, queryable, zero-row run database).
    if connection is None:
        connection = RunStore().connection
    return ExportContext(
        dataframe=dataframe, output_dir=output_dir, base_name="out",
        merge=True, preset_name=None, primary_csv=None, connection=connection,
    )


def _store_with_assertions(count: int) -> RunStore:
    """A RunStore holding ``count`` assertions from one record — the run database the ``sqlite``
    export persists."""
    store = RunStore()
    store.add_source(SourceRow(source_id="s1"))
    store.add_record(RecordRow(
        source_record_uid="r1", source_id="s1", line_no=1,
        input_record_id="t#1", recovery_state=None, raw={"City": "Geneva"}))
    for index in range(count):
        store.add_assertion(AssertionRow(
            row_uid=f"u{index}", source_record_uid="r1",
            assertion=SpatioTemporalAssertion(entity=Entity(entity=f"e{index}")),
            attribution_origin="preset_mapped"))
    return store


@pytest.fixture
def sample_df() -> pd.DataFrame:
    # Row 0 has coordinates + a valid IMEI embedded in entity; row 1 has neither.
    return pd.DataFrame({
        "linked_entity": ["subjA", "subjA"],
        "entity": ["dev_490154203237518_x", "no_imei_here"],
        "latitude_wgs84": [46.5197, None],
        "longitude_wgs84": [6.6323, None],
        "time_lower_unix_utc_us": [1765026000000000, 1765031400000000],
        "utc_offset_hours": [1.0, 0.0],
        "time_lower_source_field": ["TS", "TS"],
        "time_upper_source_field": ["TS", "TS"],
        "horizontal_speed_kmh": [10.8, None],
        "horizontal_speed_accuracy_kmh": [2.0, None],
        "horizontal_accuracy_m": [12.0, None],
        "raw_source_path": ["/x/Cache.sqlite", "/x/Cache.sqlite"],
        "input_file_path": ["in.csv", "in.csv"],
        "source_label": ["ZLOC", "ZLOC"],
        "preset_name": ["P", "P"],
    })


# --- registry / runner ------------------------------------------------------

def test_register_available_and_run(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    @register_exporter("_test_ok", "Test OK", "unit test exporter")
    def _exporter(ctx: ExportContext) -> list[Path]:
        path = ctx.output_dir / "z.txt"
        path.write_text("hi", encoding="utf-8")
        return [path]

    try:
        assert any(spec.name == "_test_ok" for spec in available_exporters())
        written = run_exporters(["_test_ok"], _ctx(sample_df, tmp_path))
        assert len(written) == 1 and written[0].exists()
    finally:
        _registry._REGISTRY.pop("_test_ok", None)


def test_unknown_export_is_skipped_not_raised(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    assert run_exporters(["does_not_exist"], _ctx(sample_df, tmp_path)) == []


def test_failing_exporter_is_isolated(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    @register_exporter("_test_boom", "Boom", "raises")
    def _boom(ctx: ExportContext) -> list[Path]:
        raise RuntimeError("deliberate")

    @register_exporter("_test_ok2", "OK2", "writes")
    def _ok(ctx: ExportContext) -> list[Path]:
        path = ctx.output_dir / "ok2.txt"
        path.write_text("y", encoding="utf-8")
        return [path]

    try:
        written = run_exporters(["_test_boom", "_test_ok2"], _ctx(sample_df, tmp_path))
        assert len(written) == 1   # the failure is skipped, the following exporter still runs
    finally:
        _registry._REGISTRY.pop("_test_boom", None)
        _registry._REGISTRY.pop("_test_ok2", None)


def test_duplicate_registration_is_a_hard_error() -> None:
    @register_exporter("_test_dup", "Dup", "")
    def _one(ctx: ExportContext) -> list[Path]:
        return []
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_exporter("_test_dup", "Dup", "")(lambda ctx: [])
    finally:
        _registry._REGISTRY.pop("_test_dup", None)


# --- drop-in plugin discovery ----------------------------------------------

def test_load_plugins_discovers_dropin(tmp_path: Path) -> None:
    (tmp_path / "myplug.py").write_text(
        "from model_atlas.exporters import register_exporter\n"
        "@register_exporter('_test_plug', 'Plug', 'drop-in')\n"
        "def export(ctx):\n"
        "    p = ctx.output_dir / 'plug.txt'\n"
        "    p.write_text('ok', encoding='utf-8')\n"
        "    return [p]\n",
        encoding="utf-8",
    )
    try:
        load_plugins(tmp_path)
        assert any(spec.name == "_test_plug" for spec in available_exporters())
    finally:
        _registry._REGISTRY.pop("_test_plug", None)


def test_broken_plugin_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("this is not valid python !!!\n", encoding="utf-8")
    load_plugins(tmp_path)   # must not raise


# --- built-in exporters -----------------------------------------------------

def test_builtin_sqlite(tmp_path: Path) -> None:
    # The sqlite export is now the FULL run database: every relational table + the assertion_flat
    # view, backed up from the store's connection (not a single 'assertions' table dump).
    store = _store_with_assertions(2)
    ctx = _ctx(store.to_flat_frame(include_source_columns=False, source_column_prefix="orig_"),
               tmp_path, connection=store.connection)
    [out] = run_exporters(["sqlite"], ctx)
    assert out.name == "out.matlas.sqlite"
    connection = sqlite3.connect(out)
    try:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        # Relational tables AND the canonical view all survive the backup.
        assert {"run", "source", "record", "entity", "position", "timespan",
                "assertion", "assertion_flat"} <= names
        assertions = connection.execute("SELECT COUNT(*) FROM assertion").fetchone()[0]
        flat = connection.execute("SELECT COUNT(*) FROM assertion_flat").fetchone()[0]
    finally:
        connection.close()
    assert assertions == flat == 2


def test_builtin_geojson_only_located_rows(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    [out] = run_exporters(["geojson"], _ctx(sample_df, tmp_path))
    data = json.loads(out.read_text(encoding="utf-8-sig"))
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 1   # only the row with coordinates
    assert data["features"][0]["geometry"]["coordinates"] == [6.6323, 46.5197]


def test_builtin_xlsx(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    [out] = run_exporters(["xlsx"], _ctx(sample_df, tmp_path))
    assert len(pd.read_excel(out)) == 2


def test_builtin_kml(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    [out] = run_exporters(["kml"], _ctx(sample_df, tmp_path))
    text = out.read_text(encoding="utf-8")
    assert text.count("<Placemark>") == 1


def _read_geovis(path: Path) -> tuple[list[str], list[list[str]]]:
    """Parse a GeoVisualizer CSV: ';' delimiter, minimal quoting (multi-line Description quoted)."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    return rows[0], rows[1:]


def test_geovisualizer_columns_and_mapping(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    [out] = run_exporters(["geovisualizer"], _ctx(sample_df, tmp_path))
    header, data = _read_geovis(out)
    assert header == [
        "Target", "MSISDN", "IMEI", "Timestamp (local)", "Cell-ID", "Azimuth", "Address",
        "Latitude", "Longitude", "Description", "Source", "orig. Time", "Type", "Confidence", "Precision",
    ]
    first = dict(zip(header, data[0]))
    assert first["Target"] == "subjA"
    assert first["MSISDN"] == ""
    assert first["IMEI"] == "490154203237518"                     # picked from the entity, Luhn-valid
    assert first["Timestamp (local)"] == "06.12.2025 14:00:00.000000+01:00"   # dd.mm.yyyy + offset
    assert "(mATLAS)" in first["Description"] and "Speed: 10.8 km/h" in first["Description"]
    assert "Cache.sqlite" in first["Source"] and "in.csv" in first["Source"]
    assert first["Type"] == "P"                                   # preset_name (no source_label fallback)
    assert first["Confidence"] == ""
    second = dict(zip(header, data[1]))
    assert second["IMEI"] == ""                                   # no valid IMEI token in the entity


# --- pipeline integration ---------------------------------------------------

def test_pipeline_writes_extras(tmp_path: Path) -> None:
    from model_atlas.pipeline import process

    golden = Path(__file__).resolve().parent / "fixtures" / "golden"
    out = tmp_path / "out.csv"
    result = process(
        golden / "cached.csv", golden / "secondary_cached.preset.yaml", out,
        linked_entity="subject", exports=["sqlite", "geovisualizer"],
    )
    names = {path.name for path in result.output_extras}
    assert any(name.endswith(".matlas.sqlite") for name in names)
    assert any(name.endswith(".geovisualizer.csv") for name in names)
    assert all(path.exists() for path in result.output_extras)


# --- edge cases: runner / plugins ------------------------------------------

def test_run_exporters_none_and_empty_names(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    assert run_exporters(None, _ctx(sample_df, tmp_path)) == []
    assert run_exporters([], _ctx(sample_df, tmp_path)) == []


def test_run_exporters_mixed_known_and_unknown(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    # An unknown name between two valid ones is skipped; the valid ones still write.
    written = run_exporters(["sqlite", "nope", "geojson"], _ctx(sample_df, tmp_path))
    assert len(written) == 2


def test_load_plugins_missing_dir_is_noop(tmp_path: Path) -> None:
    load_plugins(tmp_path / "does_not_exist")   # must not raise


def test_load_plugins_ignores_underscore_files(tmp_path: Path) -> None:
    # A leading-underscore file would raise on import if loaded; it must be ignored.
    (tmp_path / "_hidden.py").write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
    load_plugins(tmp_path)


def test_load_plugins_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "p.py").write_text(
        "from model_atlas.exporters import register_exporter\n"
        "@register_exporter('_test_idem', 'Idem', '')\n"
        "def export(ctx):\n"
        "    return []\n",
        encoding="utf-8",
    )
    try:
        load_plugins(tmp_path)
        load_plugins(tmp_path)   # second call must NOT re-import (would be a duplicate-name error)
        assert any(spec.name == "_test_idem" for spec in available_exporters())
    finally:
        _registry._REGISTRY.pop("_test_idem", None)


def test_plugin_name_colliding_with_builtin_is_logged_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text(
        "from model_atlas.exporters import register_exporter\n"
        "@register_exporter('sqlite', 'Impostor', '')\n"   # collides with the builtin
        "def export(ctx):\n"
        "    return []\n",
        encoding="utf-8",
    )
    load_plugins(tmp_path)                       # the ValueError is caught inside load_plugins
    assert get_exporter("sqlite").label == "Run database"   # builtin intact, not overridden


# --- edge cases: exporters on sparse / empty frames ------------------------

def _schema_zero_rows() -> pd.DataFrame:
    return pd.DataFrame({
        column: pd.Series(dtype="object") for column in
        ("linked_entity", "entity", "latitude_wgs84", "longitude_wgs84",
         "time_lower_unix_utc_us", "utc_offset_hours")
    })


@pytest.mark.parametrize("name", ["sqlite", "geojson", "xlsx", "kml", "geovisualizer"])
def test_builtins_handle_zero_row_frame(tmp_path: Path, name: str) -> None:
    [out] = run_exporters([name], _ctx(_schema_zero_rows(), tmp_path))
    assert out.exists()


def test_geovisualizer_minimal_columns_no_crash(tmp_path: Path) -> None:
    # Only Target + coordinates present; every other source column is absent.
    frame = pd.DataFrame({"linked_entity": ["x"], "latitude_wgs84": [1.0], "longitude_wgs84": [2.0]})
    [out] = run_exporters(["geovisualizer"], _ctx(frame, tmp_path))
    header, data = _read_geovis(out)
    row = dict(zip(header, data[0]))
    assert row["Target"] == "x"
    assert row["IMEI"] == "" and row["Timestamp (local)"] == "" and row["Source"] == ""
    # No offset column at all => unknown => the warning leads, then 'Speed: null', then (mATLAS).
    assert row["Description"] == f"{_builtin._OFFSET_UNKNOWN_NOTE} | Speed: null | (mATLAS)"


def test_geovisualizer_string_coordinates_coerced(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "linked_entity": ["x"], "latitude_wgs84": ["46.5"], "longitude_wgs84": ["6.6"],
        "utc_offset_hours": [0.0],
    })
    [out] = run_exporters(["geovisualizer"], _ctx(frame, tmp_path))
    header, data = _read_geovis(out)
    row = dict(zip(header, data[0]))
    assert float(row["Latitude"]) == 46.5 and float(row["Longitude"]) == 6.6


def test_geovisualizer_warns_when_offset_unknown(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "linked_entity": ["x"], "latitude_wgs84": [1.0], "longitude_wgs84": [2.0],
        "time_lower_unix_utc_us": [1765026000000000], "utc_offset_hours": ["N/A"],
    })
    [out] = run_exporters(["geovisualizer"], _ctx(frame, tmp_path))
    header, data = _read_geovis(out)
    row = dict(zip(header, data[0]))
    assert row["Description"].startswith(_builtin._OFFSET_UNKNOWN_NOTE)
    assert row["Timestamp (local)"].endswith("+00:00")   # placeholder suffix, flagged as unknown


def test_sqlite_rerun_overwrites_cleanly(tmp_path: Path) -> None:
    store = _store_with_assertions(2)
    frame = store.to_flat_frame(include_source_columns=False, source_column_prefix="orig_")
    run_exporters(["sqlite"], _ctx(frame, tmp_path, connection=store.connection))
    [out] = run_exporters(["sqlite"], _ctx(frame, tmp_path, connection=store.connection))  # same path
    connection = sqlite3.connect(out)
    try:
        count = connection.execute("SELECT COUNT(*) FROM assertion").fetchone()[0]
    finally:
        connection.close()
    assert count == 2   # replaced (backed up afresh), not appended


def test_geojson_properties_exclude_coords(tmp_path: Path, sample_df: pd.DataFrame) -> None:
    [out] = run_exporters(["geojson"], _ctx(sample_df, tmp_path))
    properties = json.loads(out.read_text(encoding="utf-8-sig"))["features"][0]["properties"]
    assert "latitude_wgs84" not in properties and "longitude_wgs84" not in properties
    assert properties["linked_entity"] == "subjA"


# --- edge cases: GeoVisualizer field helpers -------------------------------

def test_pick_imei_splits_on_dash_underscore_space() -> None:
    assert _builtin._pick_imei("dev_490154203237518_x") == "490154203237518"
    assert _builtin._pick_imei("dev-490154203237518-x") == "490154203237518"
    assert _builtin._pick_imei("dev 490154203237518 x") == "490154203237518"
    assert _builtin._pick_imei("490154203237518") == "490154203237518"     # bare value
    assert _builtin._pick_imei("foo_bar-490154203237518") == "490154203237518"
    assert _builtin._pick_imei(None) == ""
    assert _builtin._pick_imei("") == ""
    assert _builtin._pick_imei("dev_123_x") == ""                          # too short
    assert _builtin._pick_imei("490154203237517") == ""                    # 15 digits, Luhn-invalid
    assert _builtin._pick_imei("4901542032375180") == ""                   # 16 digits
    assert _builtin._pick_imei("49015420323751a") == ""                    # non-digit


def test_local_timestamp_format_and_offsets() -> None:
    us = 1765026000000000   # 2025-12-06 13:00:00 UTC
    assert _builtin._local_timestamp(us, 1.0) == "06.12.2025 14:00:00.000000+01:00"
    assert _builtin._local_timestamp(us, 0.0) == "06.12.2025 13:00:00.000000+00:00"
    assert _builtin._local_timestamp(us, -5.0) == "06.12.2025 08:00:00.000000-05:00"
    assert _builtin._local_timestamp(us, 5.5) == "06.12.2025 18:30:00.000000+05:30"   # fractional zone
    assert _builtin._local_timestamp(us, "N/A") == "06.12.2025 13:00:00.000000+00:00"  # unknown => UTC
    assert _builtin._local_timestamp(None, 1.0) == ""
    assert _builtin._local_timestamp(float("nan"), 1.0) == ""
    assert _builtin._local_timestamp("not-a-number", 1.0) == ""


def test_local_iso_still_used_by_kml() -> None:
    us = 1765026000000000
    assert _builtin._local_iso(us, 1.0) == "2025-12-06T14:00:00+01:00"
    assert _builtin._local_iso(None, 1.0) == ""


def test_description_format() -> None:
    both = {"utc_offset_hours": 1.0, "horizontal_speed_kmh": 10.8, "horizontal_speed_accuracy_kmh": 2.0}
    assert _builtin._geovis_description(both, "TS", None) == "Speed: 10.8 km/h (±2 km/h) | TS | (mATLAS)"

    no_accuracy = {"utc_offset_hours": 1.0, "horizontal_speed_kmh": 5.0, "horizontal_speed_accuracy_kmh": None}
    assert _builtin._geovis_description(no_accuracy, "TS", None) == "Speed: 5 km/h | TS | (mATLAS)"

    no_speed = {"utc_offset_hours": 1.0}
    assert _builtin._geovis_description(no_speed, "TS", None) == "Speed: null | TS | (mATLAS)"

    # An interval bound adds 'Range start' / 'Range end' before the (mATLAS) signature.
    assert _builtin._geovis_description({"utc_offset_hours": 1.0}, "Lower", "start") == (
        "Speed: null | Lower | Range start | (mATLAS)")
    assert _builtin._geovis_description({"utc_offset_hours": 1.0}, "Upper", "end") == (
        "Speed: null | Upper | Range end | (mATLAS)")

    # Unknown offset => the warning is the FIRST part, before Speed.
    unknown = {"utc_offset_hours": "N/A", "horizontal_speed_kmh": 3.0}
    assert _builtin._geovis_description(unknown, "TS", None) == (
        f"{_builtin._OFFSET_UNKNOWN_NOTE} | Speed: 3 km/h | TS | (mATLAS)")


def test_description_has_no_newlines() -> None:
    # The target Java importer cannot render newlines; the description must stay single-line.
    text = _builtin._geovis_description({"utc_offset_hours": "N/A", "horizontal_speed_kmh": 3.0}, "TS", "start")
    assert "\n" not in text and "\r" not in text


def test_time_emissions_instant_vs_interval() -> None:
    instant = {"time_lower_unix_utc_us": 100, "time_upper_unix_utc_us": 100,
               "time_lower_source_field": "TS", "time_upper_source_field": "TS"}
    assert _builtin._time_emissions(instant) == [(100, "TS", None)]

    interval = {"time_lower_unix_utc_us": 100, "time_upper_unix_utc_us": 200,
                "time_lower_source_field": "Start", "time_upper_source_field": "End"}
    assert _builtin._time_emissions(interval) == [(100, "Start", "start"), (200, "End", "end")]

    only_lower = {"time_lower_unix_utc_us": 100, "time_lower_source_field": "TS"}
    assert _builtin._time_emissions(only_lower) == [(100, "TS", None)]

    no_time = {"time_lower_source_field": "TS"}
    assert _builtin._time_emissions(no_time) == [(None, "TS", None)]


def test_geovisualizer_interval_expands_to_two_rows(tmp_path: Path) -> None:
    frame = pd.DataFrame({
        "linked_entity": ["x"], "latitude_wgs84": [1.0], "longitude_wgs84": [2.0],
        "utc_offset_hours": [1.0], "preset_name": ["P"],
        "time_lower_unix_utc_us": [1765026000000000], "time_upper_unix_utc_us": [1765029600000000],
        "time_lower_source_field": ["Start TS"], "time_upper_source_field": ["End TS"],
    })
    [out] = run_exporters(["geovisualizer"], _ctx(frame, tmp_path))
    header, data = _read_geovis(out)
    assert len(data) == 2                                     # one row per time bound
    first, second = dict(zip(header, data[0])), dict(zip(header, data[1]))
    assert "Start TS" in first["Description"] and "Range start" in first["Description"]
    assert "End TS" in second["Description"] and "Range end" in second["Description"]
    assert first["Timestamp (local)"] != second["Timestamp (local)"]
    assert first["Latitude"] == second["Latitude"] == "1.0"  # shared position across both bounds


def test_source_formats() -> None:
    assert _builtin._geovis_source({"raw_source_path": "/a", "input_file_path": "b.csv"}) == "/a (b.csv)"
    assert _builtin._geovis_source({"raw_source_path": "/a", "input_file_path": None}) == "/a"
    assert _builtin._geovis_source({"raw_source_path": None, "input_file_path": "b.csv"}) == "b.csv"
    assert _builtin._geovis_source({}) == ""


def test_geovisualizer_type_is_preset_name_only(tmp_path: Path) -> None:
    # Type is the preset name; a present source_label must NOT be used.
    frame = pd.DataFrame({
        "linked_entity": ["x"], "latitude_wgs84": [1.0], "longitude_wgs84": [2.0],
        "utc_offset_hours": [0.0], "source_label": ["ShouldBeIgnored"], "preset_name": ["ThePreset"],
    })
    [out] = run_exporters(["geovisualizer"], _ctx(frame, tmp_path))
    header, data = _read_geovis(out)
    assert dict(zip(header, data[0]))["Type"] == "ThePreset"
