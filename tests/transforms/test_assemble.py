"""Tests for the v3 assembly engine (transforms.assemble).

Covers fan-out across assertions, NaN normalisation, source_record_uid sourcing +
uniqueness guard, glob columns, header() timezone capture, unit conversion + inferred
cast, regex extract, and lat/lon source-field capture.
Used by:    pytest.
Depends on: transforms.assemble, presets.spec, pandas, pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.transforms.assemble import BuildEnv, build_rows, to_records

_ENV = BuildEnv(input_file_path="/dev/v.csv", input_file_name="v.csv", source_fingerprint="fp123", source_file_path="/dev/file", source_tier="secondary")

_VISITS = """
preset: {id: t.visits, name: Visits, version: 1.0, tier: secondary}
input_selector: {format: csv, name: v.csv}
source_record_uid: column(ItemID)
common:
  entity: const(device)
  input_record_id: column(Loc)
assertions:
  - position:
      latitude_wgs84: column(Lat)
      longitude_wgs84: column(Lon)
    time:
      interval: {lower: column(Entry), upper: column(Exit)}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: continuous_during_interval}
  - position:
      latitude_wgs84: column(Lat)
      longitude_wgs84: column(Lon)
    time:
      instant: column(Created)
    links: {entity_position: at, entity_time: recorded_at, spatial_temporal: instant}
"""


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("t.yaml"))


def test_two_assertions_share_source_row_id_and_capture_provenance() -> None:
    records = [{"Lat": "1.5", "Lon": "2.5", "Entry": "100", "Exit": "200", "Created": "150",
                "Loc": "ZTABLE(1)", "ItemID": "A1"}]
    frame, warnings = build_rows(records, _preset(_VISITS), _ENV)
    assert warnings == []
    assert len(frame) == 2
    interval, instant = frame.iloc[0], frame.iloc[1]
    assert interval["source_record_uid"] == instant["source_record_uid"] == "A1"
    # row_uid differs per output row even though they share the source record.
    assert interval["row_uid"] != instant["row_uid"]
    assert interval["latitude_wgs84"] == 1.5 and interval["longitude_wgs84"] == 2.5   # inferred float cast
    assert interval["time_lower_unix_us"] == 100 and interval["time_upper_unix_us"] == 200  # raw us
    assert interval["spatial_temporal_link"] == "continuous_during_interval"
    assert instant["time_lower_unix_us"] == instant["time_upper_unix_us"] == 150
    assert instant["entity_time_link"] == "recorded_at"
    assert interval["time_lower_raw"] == "100" and interval["time_lower_source_field"] == "Entry"
    # lat/lon source columns captured automatically.
    assert interval["latitude_source_field"] == "Lat" and interval["longitude_source_field"] == "Lon"


def test_source_columns_passthrough_on_every_pivoted_row() -> None:
    # A pivoted source row (interval + instant) must repeat its orig_ columns on BOTH
    # output rows, including a column no mapping consumed ("City").
    record = {"Lat": "1.5", "Lon": "2.5", "Entry": "100", "Exit": "200", "Created": "150",
              "Loc": "ZTABLE(1)", "ItemID": "A1", "City": "Geneva"}
    columns = ["Lat", "Lon", "Entry", "Exit", "Created", "Loc", "ItemID", "City"]
    frame, _ = build_rows([record], _preset(_VISITS), _ENV, columns=columns)
    assert len(frame) == 2
    # orig_<col> appended after the canonical schema, in source order.
    assert list(frame.columns)[-len(columns):] == [f"orig_{c}" for c in columns]
    for _, row in frame.iterrows():
        assert row["orig_City"] == "Geneva"          # unmapped, verbatim, on every row
        assert row["orig_Lat"] == "1.5"              # mapped column also kept verbatim
    # Opt-out drops the passthrough columns.
    frame_off, _ = build_rows([record], _preset(_VISITS), _ENV, columns=columns, include_source_columns=False)
    assert not any(f"orig_{c}" in frame_off.columns for c in columns)


def test_to_records_normalises_nan_to_none() -> None:
    records = to_records(pd.DataFrame({"a": [1.0, None], "b": ["x", None]}))
    assert records[1]["a"] is None and records[1]["b"] is None


def test_uniqueness_guard_fires_on_mapped_duplicate_source_record_uid() -> None:
    # A preset that MAPS source_record_uid to a non-unique column is an authoring error: two
    # rows sharing that "stable id" must raise (do not silently merge evidence).
    records = [
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "L1", "ItemID": "DUP"},
        {"Lat": "3", "Lon": "4", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "L2", "ItemID": "DUP"},
    ]
    with pytest.raises(ValueError, match="not unique"):
        build_rows(records, _preset(_VISITS), _ENV)


def test_generated_uid_disambiguates_identical_rows_by_record_number() -> None:
    # No source_record_uid mapped and IDENTICAL row data: the generated UID is keyed on the
    # physical source record number, so the two records still get distinct UIDs (no crash),
    # and every output row gets a distinct row_uid.
    text = _VISITS.replace("source_record_uid: column(ItemID)\n", "").replace("  input_record_id: column(Loc)\n", "")
    records = [
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1"},
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1"},   # identical data
    ]
    frame, _ = build_rows(records, _preset(text), _ENV)
    assert list(frame["source_record_number"]) == [1, 1, 2, 2]  # 1-based, two assertions per record
    assert len(set(frame["source_record_uid"])) == 2   # disambiguated by record number
    assert len(set(frame["row_uid"])) == 4             # every output row distinct
    # Deterministic: a re-run yields the same ids (both columns).
    frame2, _ = build_rows(records, _preset(text), _ENV)
    assert list(frame["source_record_uid"]) == list(frame2["source_record_uid"])
    assert list(frame["row_uid"]) == list(frame2["row_uid"])


def test_row_uid_is_content_addressed() -> None:
    # row_uid folds in the row's own data: changing a cell changes that row's row_uid,
    # while a row whose data is unchanged keeps its row_uid.
    text = _VISITS.replace("source_record_uid: column(ItemID)\n", "")
    base = [{"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "A"},
            {"Lat": "9", "Lon": "8", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "B"}]
    changed = [dict(base[0]), {**base[1], "Lat": "7"}]   # second record's Lat edited
    frame_a, _ = build_rows(base, _preset(text), _ENV)
    frame_b, _ = build_rows(changed, _preset(text), _ENV)
    # Record 0 is untouched -> same row_uids; record 1's data changed -> different row_uids.
    assert list(frame_a["row_uid"])[:2] == list(frame_b["row_uid"])[:2]
    assert list(frame_a["row_uid"])[2:] != list(frame_b["row_uid"])[2:]


_WILDCARD = """
preset: {id: t.wc, name: WC, version: 1.0, tier: secondary}
input_selector: {format: csv, name: w.csv}
patterns: {tz: "(?P<z>UTC[+-][0-9]{2}:[0-9]{2})"}
source_record_uid: column(ItemID)
common: {entity: const(device), input_record_id: column(Loc)}
assertions:
  - position: {latitude_wgs84: column(Lat)}
    time:
      instant: 'column("Timestamp - * (dd)")'
      format: "%d.%m.%Y %H:%M:%S.%f"
      zone: { from: 'header("Timestamp - * (dd)")', pipe: "regex(tz, group=z)" }
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_glob_column_and_header_capture_timezone() -> None:
    cols = ["Lat", "Loc", "ItemID", "Timestamp - UTC+02:00 (dd)"]
    records = [{"Lat": "1.0", "Loc": "L", "ItemID": "A", "Timestamp - UTC+02:00 (dd)": "06.12.2025 13:00:00.000"}]
    frame, warnings = build_rows(records, _preset(_WILDCARD), _ENV, columns=cols)
    row = frame.iloc[0]
    assert row["time_lower_source_field"] == "Timestamp - UTC+02:00 (dd)"
    assert row["time_zone"] == "UTC+02:00"
    assert warnings == []


def test_ambiguous_glob_is_a_hard_error() -> None:
    cols = ["Lat", "Loc", "ItemID", "Timestamp - UTC+00:00 (dd)", "Timestamp - UTC+02:00 (dd)"]
    records = [{"Lat": "1.0", "Loc": "L", "ItemID": "A",
                "Timestamp - UTC+00:00 (dd)": "06.12.2025 13:00:00.000",
                "Timestamp - UTC+02:00 (dd)": "06.12.2025 15:00:00.000"}]
    with pytest.raises(ValueError, match="multiple"):
        build_rows(records, _preset(_WILDCARD), _ENV, columns=cols)


_UNITS = """
preset: {id: t.u, name: U, version: 1.0, tier: secondary}
input_selector: {format: csv, name: u.csv}
patterns: {coords: "(?P<lat>-?[0-9.]+) - (?P<lon>-?[0-9.]+)"}
source_record_uid: column(ItemID)
common: {entity: const(device)}
assertions:
  - position:
      latitude_wgs84:  { from: column(C), extract: coords.lat }
      longitude_wgs84: { from: column(C), extract: coords.lon }
      horizontal_speed_kmh: { from: column(Spd), unit: m/s }
    time: {instant: column(TS), epoch: cocoa, zone: const(UTC)}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_unit_conversion_extract_and_cocoa_epoch() -> None:
    records = [{"C": "38.4 - -0.45", "Spd": "10", "TS": "0", "ItemID": "A"}]
    frame, warnings = build_rows(records, _preset(_UNITS), _ENV)
    row = frame.iloc[0]
    assert row["latitude_wgs84"] == 38.4 and row["longitude_wgs84"] == -0.45
    assert row["latitude_source_field"] == "C" and row["longitude_source_field"] == "C"
    assert row["horizontal_speed_kmh"] == 36.0                  # 10 m/s -> km/h
    assert row["time_lower_unix_us"] == 978_307_200 * 1_000_000  # cocoa 0
    assert warnings == []
