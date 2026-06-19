"""Tests for the v3 assembly engine (transforms.assemble).

Covers fan-out across assertions, NaN normalisation, record_uid sourcing +
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

_ENV = BuildEnv(input_file="v.csv", source_fingerprint="fp123", source_file_path="/dev/file", source_tier="secondary")

_VISITS = """
preset: {id: t.visits, name: Visits, version: 1.0, tier: secondary}
match: {type: csv, as_file: v.csv}
record_uid: column(ItemID)
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
    assert interval["record_uid"] == instant["record_uid"] == "A1"
    assert interval["latitude_wgs84"] == 1.5 and interval["longitude_wgs84"] == 2.5   # inferred float cast
    assert interval["time_lower_unix_us"] == 100 and interval["time_upper_unix_us"] == 200  # raw us
    assert interval["spatial_temporal_link"] == "continuous_during_interval"
    assert instant["time_lower_unix_us"] == instant["time_upper_unix_us"] == 150
    assert instant["entity_time_link"] == "recorded_at"
    assert interval["time_lower_raw"] == "100" and interval["time_lower_source_field"] == "Entry"
    # lat/lon source columns captured automatically.
    assert interval["latitude_source_field"] == "Lat" and interval["longitude_source_field"] == "Lon"


def test_to_records_normalises_nan_to_none() -> None:
    records = to_records(pd.DataFrame({"a": [1.0, None], "b": ["x", None]}))
    assert records[1]["a"] is None and records[1]["b"] is None


def test_uniqueness_guard_fires_on_coarse_input_record_id() -> None:
    # No record_uid mapped, so the UID is generated over input_record_id (here mapped to a
    # non-unique Loc column) — two identical Loc values must collide and raise.
    no_uid = _VISITS.replace("record_uid: column(ItemID)\n", "")
    records = [
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "SAME"},
        {"Lat": "3", "Lon": "4", "Entry": "1", "Exit": "2", "Created": "1", "Loc": "SAME"},
    ]
    with pytest.raises(ValueError, match="not unique"):
        build_rows(records, _preset(no_uid), _ENV)


def test_generated_uid_is_unique_per_row_without_mapping() -> None:
    # No record_uid and no mapped input_record_id: the engine falls back to the row ordinal,
    # so distinct rows get distinct, deterministic UIDs (no collision).
    text = _VISITS.replace("record_uid: column(ItemID)\n", "").replace("  input_record_id: column(Loc)\n", "")
    records = [
        {"Lat": "1", "Lon": "2", "Entry": "1", "Exit": "2", "Created": "1"},
        {"Lat": "3", "Lon": "4", "Entry": "1", "Exit": "2", "Created": "1"},
    ]
    frame, _ = build_rows(records, _preset(text), _ENV)
    uids = set(frame["record_uid"])
    assert len(uids) == 2  # one uid per source row, shared across its two assertions
    # Deterministic: a re-run yields the same ids.
    frame2, _ = build_rows(records, _preset(text), _ENV)
    assert list(frame["record_uid"]) == list(frame2["record_uid"])


_WILDCARD = """
preset: {id: t.wc, name: WC, version: 1.0, tier: secondary}
match: {type: csv, as_file: w.csv}
patterns: {tz: "(?P<z>UTC[+-][0-9]{2}:[0-9]{2})"}
record_uid: column(ItemID)
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
match: {type: csv, as_file: u.csv}
patterns: {coords: "(?P<lat>-?[0-9.]+) - (?P<lon>-?[0-9.]+)"}
record_uid: column(ItemID)
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
