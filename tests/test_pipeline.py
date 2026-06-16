"""Integration tests for the pipeline: output, sidecars, frontier, and split mode.

Defines:    end-to-end runs on temp CSVs checking the 40-column output, the readable
            and PROV traceability sidecars, the frontier (known/new/drift), and the
            split (merge=False) output mode.
Used by:    pytest.
Depends on: pipeline, model.families, json.
"""
from __future__ import annotations

import json
from pathlib import Path

from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.pipeline import process

_PRESET = """
name: T
parser: {name: t, version: "1.0"}
source_tier: secondary
selectors: [{source_type: csv, file_name: "data.csv"}]
expected_columns: [Lat, Lon, TS, Loc, Item, Notes, Gone]
source_row_id: {from: Item}
common:
  entity: {value: device}
  record_locator: {from: Loc}
assertions:
  - latitude_wgs84: {from: Lat, pipe: [{cast: float}]}
    longitude_wgs84: {from: Lon, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - instant: TS
        pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
"""

_CSV = (
    "Lat,Lon,TS,Loc,Item,Notes,Mystery\n"
    "1.5,2.5,06.12.2025 13:00:00.000,L1,A,note,xyz\n"
)


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    presets = tmp_path / "presets"
    presets.mkdir()
    (presets / "data.yaml").write_text(_PRESET, encoding="utf-8")
    source = tmp_path / "data.csv"
    source.write_text(_CSV, encoding="utf-8")
    return source, presets, tmp_path / "out.csv"


def test_pipeline_output_and_readable_sidecars(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output)

    assert result.row_counts["rows"] == 1
    assert result.output_csv is not None and result.output_csv.exists()

    header = result.output_csv.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == list(OUTPUT_COLUMNS)

    trace = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert trace["tool"]["pipeline"] == "v1"
    front = trace["sources"][0]["frontier"]
    assert front["frontier_known"] == ["Notes"]    # present + expected, unmapped
    assert front["frontier_new"] == ["Mystery"]    # present, unexpected, unmapped
    assert front["drift_missing"] == ["Gone"]       # expected but absent

    warnings = json.loads(result.output_warnings.read_text(encoding="utf-8"))
    assert warnings["transform_warning_count"] == 0
    assert warnings["frontier"][0]["frontier"]["frontier_new"] == ["Mystery"]


def test_pipeline_prov_traceability(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output, traceability_format="prov")
    prov = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert "entity" in prov and "activity" in prov and "wasGeneratedBy" in prov
    assert prov["activity"]["matlas:run"]["prov:startTime"]


_PRESET_B = """
name: U
parser: {name: u, version: "1.0"}
source_tier: secondary
selectors: [{source_type: csv, file_name: "other.csv"}]
expected_columns: [Lat, Lon, TS, Loc, Item]
source_row_id: {from: Item}
common:
  entity: {value: device}
  record_locator: {from: Loc}
assertions:
  - latitude_wgs84: {from: Lat, pipe: [{cast: float}]}
    longitude_wgs84: {from: Lon, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - instant: TS
        pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
"""

_CSV_B = (
    "Lat,Lon,TS,Loc,Item\n"
    "3.0,4.0,07.12.2025 09:00:00.000,L2,B\n"
)


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
    result = process(input_folder, presets, output_folder, merge=False)

    assert result.output_csv is None
    assert len(result.output_csvs) == 2
    assert result.row_counts["rows"] == 2
    assert result.row_counts["matched"] == 2

    names = {p.stem for p in result.output_csvs}
    assert names == {"T", "U"}

    for csv in result.output_csvs:
        assert csv.exists()
        header = csv.read_text(encoding="utf-8").splitlines()[0].split(",")
        assert header == list(OUTPUT_COLUMNS)
        assert csv.with_suffix(".matlas.traceability.json").exists()
        assert csv.with_suffix(".matlas.warnings.json").exists()
