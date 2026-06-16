"""Tests for the assembly engine (transforms.assemble).

Defines:    fan-out, NaN normalisation, source_row_id sourcing, and uniqueness-guard tests.
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

_ENV = BuildEnv(acquisition_path="acq.zip", source_file_path="/dev/file", input_file="v.csv", source_tier="secondary")

_VISITS = """
name: Visits
parser: {name: visits, version: "1.0"}
source_tier: secondary
selectors: [{source_type: csv, file_name: "v.csv"}]
common:
  entity: {value: device}
  record_locator: {from: Loc}
source_row_id: {from: ItemID}
assertions:
  - latitude_wgs84: {from: Lat, pipe: [{cast: float}]}
    longitude_wgs84: {from: Lon, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - interval: {lower: Entry, upper: Exit}
        pipe: [{cast: int}]
        spatial_temporal_link: continuous_during_interval
        entity_time_link: observed_at
      - instant: Created
        pipe: [{cast: int}]
        spatial_temporal_link: instant
        entity_time_link: recorded_at
"""


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("t.yaml"))


def test_interval_and_instant_fan_out_share_source_row_id() -> None:
    records = [{"Lat": "1.5", "Lon": "2.5", "Entry": "100", "Exit": "200", "Created": "150",
                "Loc": "ZTABLE(1)", "ItemID": "A1"}]
    frame, warnings = build_rows(records, _preset(_VISITS), _ENV)
    assert warnings == []
    assert len(frame) == 2  # one interval + one instant from a single source row

    interval = frame.iloc[0]
    instant = frame.iloc[1]
    assert interval["source_row_id"] == instant["source_row_id"] == "A1"
    assert interval["latitude_wgs84"] == 1.5 and interval["longitude_wgs84"] == 2.5
    assert interval["time_lower_unix_ns"] == 100 and interval["time_upper_unix_ns"] == 200
    assert interval["spatial_temporal_link"] == "continuous_during_interval"
    assert instant["time_lower_unix_ns"] == instant["time_upper_unix_ns"] == 150
    assert instant["entity_time_link"] == "recorded_at"
    assert interval["source_tier"] == "secondary" and interval["entity"] == "device"
    # The raw timestamp and its source field are captured automatically.
    assert interval["time_lower_raw"] == "100" and interval["time_lower_source_field"] == "Entry"


def test_to_records_normalises_nan_to_none() -> None:
    frame = pd.DataFrame({"a": [1.0, None], "b": ["x", None]})
    records = to_records(frame)
    assert records[1]["a"] is None
    assert records[1]["b"] is None


def test_empty_numeric_cell_does_not_leak_nan() -> None:
    frame = pd.DataFrame({"Lat": ["1.5"], "Lon": ["2.5"], "Entry": ["100"], "Exit": ["200"],
                          "Created": ["150"], "Loc": ["L"], "ItemID": ["A"],
                          "Speed": [None]})
    records = to_records(frame)
    rows, _ = build_rows(records, _preset(_VISITS), _ENV)
    # Speed isn't mapped here, but the point is the record carried None, not nan.
    assert records[0]["Speed"] is None
    assert len(rows) == 2


_WILDCARD = """
name: WC
parser: {name: wc, version: "1.0"}
selectors: [{source_type: csv, file_name: "w.csv"}]
common:
  entity: {value: device}
  record_locator: {from: Loc}
source_row_id: {from: ItemID}
assertions:
  - latitude_wgs84: {from: Lat, pipe: [{cast: float}]}
    temporal:
      - instant: "Timestamp - * (dd)"
        pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
        time_zone:
          from_name: "Timestamp - * (dd)"
          pipe: [{regex_extract: "(UTC[+-][0-9]{2}:[0-9]{2})"}]
"""


def test_wildcard_column_and_from_name_capture_timezone() -> None:
    cols = ["Lat", "Loc", "ItemID", "Timestamp - UTC+02:00 (dd)"]
    records = [{"Lat": "1.0", "Loc": "L", "ItemID": "A", "Timestamp - UTC+02:00 (dd)": "06.12.2025 13:00:00.000"}]
    frame, warnings = build_rows(records, _preset(_WILDCARD), _ENV, columns=cols)
    row = frame.iloc[0]
    # The glob matched the timezone-bearing column and parsed its value to nanoseconds...
    assert row["time_lower_unix_ns"] is not None and row["time_lower_unix_ns"] % 1_000_000 == 0
    assert row["time_lower_source_field"] == "Timestamp - UTC+02:00 (dd)"
    # ...and from_name + regex_extract pulled the timezone out of the column NAME into time_zone.
    assert row["time_zone"] == "UTC+02:00"
    assert warnings == []


def test_ambiguous_wildcard_is_a_hard_error() -> None:
    cols = ["Lat", "Loc", "ItemID", "Timestamp - UTC+00:00 (dd)", "Timestamp - UTC+02:00 (dd)"]
    records = [{"Lat": "1.0", "Loc": "L", "ItemID": "A",
                "Timestamp - UTC+00:00 (dd)": "06.12.2025 13:00:00.000",
                "Timestamp - UTC+02:00 (dd)": "06.12.2025 15:00:00.000"}]
    with pytest.raises(ValueError, match="multiple"):
        build_rows(records, _preset(_WILDCARD), _ENV, columns=cols)


def test_uniqueness_guard_fires_on_coarse_record_locator() -> None:
    no_item_id = _VISITS.replace("source_row_id: {from: ItemID}\n", "")
    records = [
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "SAME"},
        {"Lat": "3", "Lon": "4", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "SAME"},
    ]
    with pytest.raises(ValueError, match="not unique"):
        build_rows(records, _preset(no_item_id), _ENV)
