"""Integration tests for the pipeline: output, sidecars, frontier, and split mode (v3).

Used by:    pytest.
Depends on: pipeline, model.families, json.
"""
from __future__ import annotations

import json
from pathlib import Path

from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.pipeline import process

_PRESET = """
preset: {id: t.t, name: T, version: 1.0, tier: secondary}
match: {type: csv, as_file: data.csv}
expected_columns: [Lat, Lon, TS, Loc, Item, Notes, Gone]
record_uid: column(Item)
common: {entity: const(device), input_record_id: column(Loc)}
assertions:
  - position: {latitude_wgs84: column(Lat), longitude_wgs84: column(Lon)}
    time: {instant: column(TS), format: "%d.%m.%Y %H:%M:%S.%f"}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV = "Lat,Lon,TS,Loc,Item,Notes,Mystery\n1.5,2.5,06.12.2025 13:00:00.000,L1,A,note,xyz\n"


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "data.yaml").write_text(_PRESET, encoding="utf-8")
    (tmp_path / "data.csv").write_text(_CSV, encoding="utf-8")
    return tmp_path / "data.csv", presets, tmp_path / "out.csv"


def test_pipeline_output_and_readable_sidecars(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output, linked_entity="subject")

    assert result.row_counts["rows"] == 1
    assert result.output_csv is not None and result.output_csv.exists()
    header = result.output_csv.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == list(OUTPUT_COLUMNS)

    trace = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    front = trace["sources"][0]["frontier"]
    assert front["frontier_known"] == ["Notes"]    # present + expected, unmapped
    assert front["frontier_new"] == ["Mystery"]    # present, unexpected, unmapped
    assert front["drift_missing"] == ["Gone"]       # expected but absent
    assert front["mapped_absent"] == []

    warnings = json.loads(result.output_warnings.read_text(encoding="utf-8"))
    assert warnings["transform_warning_count"] == 0


def test_pipeline_prov_traceability(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output, linked_entity="subject", traceability_format="prov")
    prov = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert "entity" in prov and "activity" in prov and "wasGeneratedBy" in prov
    assert prov["activity"]["matlas:run"]["prov:startTime"]


_PRESET_B = """
preset: {id: t.u, name: U, version: 1.0, tier: secondary}
match: {type: csv, as_file: other.csv}
record_uid: column(Item)
common: {entity: const(device), input_record_id: column(Loc)}
assertions:
  - position: {latitude_wgs84: column(Lat), longitude_wgs84: column(Lon)}
    time: {instant: column(TS), format: "%d.%m.%Y %H:%M:%S.%f"}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV_B = "Lat,Lon,TS,Loc,Item\n3.0,4.0,07.12.2025 09:00:00.000,L2,B\n"


def _setup_two_presets(tmp_path: Path) -> tuple[Path, Path, Path]:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "data.yaml").write_text(_PRESET, encoding="utf-8")
    (presets / "other.yaml").write_text(_PRESET_B, encoding="utf-8")
    (tmp_path / "data.csv").write_text(_CSV, encoding="utf-8")
    (tmp_path / "other.csv").write_text(_CSV_B, encoding="utf-8")
    return tmp_path, presets, tmp_path / "out"


def test_pipeline_split_mode(tmp_path: Path) -> None:
    input_folder, presets, output_folder = _setup_two_presets(tmp_path)
    result = process(input_folder, presets, output_folder, linked_entity="subject", merge=False)

    assert result.output_csv is None
    assert len(result.output_csvs) == 2
    assert result.row_counts["rows"] == 2 and result.row_counts["matched"] == 2
    assert {p.stem for p in result.output_csvs} == {"T", "U"}
    for csv in result.output_csvs:
        assert csv.exists()
        header = csv.read_text(encoding="utf-8").splitlines()[0].split(",")
        assert header == list(OUTPUT_COLUMNS)
        assert csv.with_suffix(".matlas.traceability.json").exists()
        assert csv.with_suffix(".matlas.warnings.json").exists()
