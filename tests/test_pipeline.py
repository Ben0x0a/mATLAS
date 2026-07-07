"""Integration tests for the pipeline: output, sidecars, frontier, and split mode (v3).

Used by:    pytest.
Depends on: pipeline, model.families, json.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.pipeline import process


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """(header, data rows) parsed with the csv module: ';' delimiter, QUOTE_NONNUMERIC quoting."""
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    return (rows[0], rows[1:]) if rows else ([], [])

_PRESET = """
preset: {id: t.t, name: T, version: 1.0, tier: secondary}
input_selector: {format: csv, name: data.csv}
expected_fields: [Lat, Lon, TS, Loc, Item, Notes, Gone]
source_record_uid: field(Item)
common: {entity: const(device), input_record_id: field(Loc)}
assertions:
  - position: {latitude_wgs84: field(Lat), longitude_wgs84: field(Lon)}
    time: {instant: field(TS), format: "%d.%m.%Y %H:%M:%S.%f"}
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
    header, data = _read_csv(result.output_csv)
    # Canonical columns first, then an orig_<col> passthrough for every source column.
    assert header[:len(OUTPUT_COLUMNS)] == list(OUTPUT_COLUMNS)
    assert header[len(OUTPUT_COLUMNS):] == [
        "orig_Lat", "orig_Lon", "orig_TS", "orig_Loc", "orig_Item", "orig_Notes", "orig_Mystery"
    ]
    # Passthrough values are verbatim, including the unmapped columns.
    row = dict(zip(header, data[0]))
    assert row["orig_Notes"] == "note" and row["orig_Mystery"] == "xyz"

    trace = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    front = trace["sources"][0]["frontier"]
    assert front["frontier_known"] == ["Notes"]    # present + expected, unmapped
    assert front["frontier_new"] == ["Mystery"]    # present, unexpected, unmapped
    assert front["drift_missing"] == ["Gone"]       # expected but absent
    assert front["mapped_absent"] == []

    warnings = json.loads(result.output_warnings.read_text(encoding="utf-8"))
    assert warnings["transform_warning_count"] == 0


def test_process_reports_progress_phases(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    phases: list[str] = []
    process(source, presets, output, linked_entity="subject",
            exports=["sqlite"], progress=phases.append)
    # Each phase is announced at its start; a long run therefore never looks stalled.
    assert "Loading presets" in phases
    assert "Discovering sources" in phases
    assert "Extracting & mapping" in phases
    assert any(phase.startswith("Untangling") for phase in phases)
    assert "Additional exports" in phases


def test_process_progress_is_optional(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    # No progress callback: must run exactly as before.
    result = process(source, presets, output, linked_entity="subject")
    assert result.row_counts["rows"] == 1


def test_pipeline_prov_traceability(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output, linked_entity="subject", traceability_format="prov")
    prov = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert "entity" in prov and "activity" in prov and "wasGeneratedBy" in prov
    assert prov["activity"]["matlas:run"]["prov:startTime"]


_PRESET_B = """
preset: {id: t.u, name: U, version: 1.0, tier: secondary}
input_selector: {format: csv, name: other.csv}
source_record_uid: field(Item)
common: {entity: const(device), input_record_id: field(Loc)}
assertions:
  - position: {latitude_wgs84: field(Lat), longitude_wgs84: field(Lon)}
    time: {instant: field(TS), format: "%d.%m.%Y %H:%M:%S.%f"}
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


def test_pipeline_no_source_columns(tmp_path: Path) -> None:
    source, presets, output = _setup(tmp_path)
    result = process(source, presets, output, linked_entity="subject", include_source_columns=False)

    header, _ = _read_csv(result.output_csv)
    assert header == list(OUTPUT_COLUMNS)  # canonical only, no orig_ passthrough columns


def test_pipeline_merge_unions_raw_columns(tmp_path: Path) -> None:
    # Two sources with different source columns: the merged CSV's raw_ columns are the
    # union, and a row from the source lacking a column is blank there (not dropped).
    input_folder, presets, output_folder = _setup_two_presets(tmp_path)
    out = tmp_path / "merged.csv"
    result = process(input_folder, presets, out, linked_entity="subject")

    header, data = _read_csv(result.output_csv)
    # orig_Notes / orig_Mystery exist only in source A; common columns appear once.
    assert "orig_Notes" in header and "orig_Mystery" in header
    assert header.count("orig_Lat") == 1
    rows = [dict(zip(header, values)) for values in data]
    by_item = {r["orig_Item"]: r for r in rows}
    assert by_item["A"]["orig_Notes"] == "note"
    assert by_item["B"]["orig_Notes"] == ""   # source B has no Notes column


def test_pipeline_split_mode(tmp_path: Path) -> None:
    input_folder, presets, output_folder = _setup_two_presets(tmp_path)
    result = process(input_folder, presets, output_folder, linked_entity="subject", merge=False)

    assert result.output_csv is None
    assert len(result.output_csvs) == 2
    assert result.row_counts["rows"] == 2 and result.row_counts["matched"] == 2
    assert {p.stem for p in result.output_csvs} == {"T", "U"}
    for csv_path in result.output_csvs:
        assert csv_path.exists()
        header, _ = _read_csv(csv_path)
        assert header[:len(OUTPUT_COLUMNS)] == list(OUTPUT_COLUMNS)
        assert all(c.startswith("orig_") for c in header[len(OUTPUT_COLUMNS):])
        assert csv_path.with_suffix(".matlas.traceability.json").exists()
        assert csv_path.with_suffix(".matlas.warnings.json").exists()
