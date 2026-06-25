"""DST handling for AXIOM-style sources: [DST]/Local-Time columns resolved via the
configured local_zone (zoneinfo, per-row), with overlap/gap + mismatch warnings."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.transforms.assemble import BuildEnv, build_rows
from model_atlas.transforms.builtin import (
    local_naive_to_utc_us,
    parse_zone_token,
    zone_standard_offset_hours,
)


# --- unit: zone token parsing --------------------------------------------

def test_parse_zone_token_offset_and_dst() -> None:
    t = parse_zone_token("UTC+01:00")
    assert (t.base_offset_hours, t.dst) == (1.0, False)
    t = parse_zone_token("UTC+01:00[DST]")
    assert (t.base_offset_hours, t.dst) == (1.0, True)
    assert parse_zone_token("local").dst is True
    assert parse_zone_token("UTC").base_offset_hours == 0.0
    assert parse_zone_token("") is None


def test_zone_standard_offset() -> None:
    assert zone_standard_offset_hours("Europe/Paris") == 1.0   # CET (winter/standard)
    assert zone_standard_offset_hours("Europe/London") == 0.0
    assert zone_standard_offset_hours("Asia/Kolkata") == 5.5


# --- unit: naive-local -> UTC, with DST anomalies ------------------------

def test_local_naive_to_utc_normal_winter_summer() -> None:
    _, off_w, an_w = local_naive_to_utc_us(dt.datetime(2025, 12, 6, 13, 0), "Europe/Paris")
    _, off_s, an_s = local_naive_to_utc_us(dt.datetime(2025, 7, 6, 13, 0), "Europe/Paris")
    assert (off_w, an_w) == (1.0, None)                        # winter +1
    assert (off_s, an_s) == (2.0, None)                        # summer +2


def test_local_naive_to_utc_flags_fall_back_overlap() -> None:
    # 2025-10-26 02:30 occurs twice in Paris (03:00 -> 02:00 fall-back).
    _, _, anomaly = local_naive_to_utc_us(dt.datetime(2025, 10, 26, 2, 30), "Europe/Paris")
    assert anomaly == "ambiguous"


def test_local_naive_to_utc_flags_spring_forward_gap() -> None:
    # 2025-03-30 02:30 never exists in Paris (02:00 -> 03:00 spring-forward).
    _, _, anomaly = local_naive_to_utc_us(dt.datetime(2025, 3, 30, 2, 30), "Europe/Paris")
    assert anomaly == "imaginary"


# --- end-to-end: a [DST] source resolved per-row -------------------------

_PRESET = """
preset: {id: t.dst, name: DST, version: 1.0, tier: secondary}
input_selector: {format: csv, name: x.csv}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time:
      instant: column(ts)
      format: "%d.%m.%Y %H:%M:%S.%f"
      zone: column(tz)
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def _run(records, local_zone):
    spec = preset_spec_from_yaml(yaml.safe_load(_PRESET), Path("t.yaml"))
    env = BuildEnv(linked_entity="subj", local_zone=local_zone)
    return build_rows(records, spec, env, columns=["lat", "lon", "ts", "tz"])


def test_dst_source_resolves_per_row_offset_and_utc() -> None:
    # Same wall-clock 13:00 in Paris is +1 in winter, +2 in summer -> different UTC.
    winter = {"lat": "1", "lon": "2", "ts": "06.12.2025 13:00:00.000", "tz": "UTC+01:00[DST]"}
    summer = {"lat": "3", "lon": "4", "ts": "06.07.2025 13:00:00.000", "tz": "UTC+01:00[DST]"}
    frame, warnings = _run([winter, summer], "Europe/Paris")
    assert list(frame["utc_offset_hours"]) == [1.0, 2.0]
    # winter 13:00 +1 -> 12:00 UTC; summer 13:00 +2 -> 11:00 UTC (one hour earlier).
    w = dt.datetime(2025, 12, 6, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1_000_000
    s = dt.datetime(2025, 7, 6, 11, 0, tzinfo=dt.timezone.utc).timestamp() * 1_000_000
    assert frame["time_lower_unix_utc_us"].tolist() == [int(w), int(s)]
    assert warnings == []


def test_dst_source_without_local_zone_warns_and_uses_base() -> None:
    rec = {"lat": "1", "lon": "2", "ts": "06.07.2025 13:00:00.000", "tz": "UTC+01:00[DST]"}
    frame, warnings = _run([rec], None)                        # no configured zone
    assert frame["utc_offset_hours"].tolist() == [1.0]         # fell back to base offset
    assert any("no local_zone configured" in w for w in warnings)


def test_base_offset_zone_mismatch_warns() -> None:
    rec = {"lat": "1", "lon": "2", "ts": "06.07.2025 13:00:00.000", "tz": "UTC+05:00[DST]"}
    _, warnings = _run([rec], "Europe/Paris")                  # header base +5 vs Paris std +1
    assert any("does not match configured local_zone" in w for w in warnings)


def test_utc_source_records_configured_zone_offset() -> None:
    # A plain UTC+00:00 source, with Paris configured, records Paris's offset (model change).
    rec = {"lat": "1", "lon": "2", "ts": "06.12.2025 12:00:00.000", "tz": "UTC+00:00"}
    frame, _ = _run([rec], "Europe/Paris")
    assert frame["utc_offset_hours"].tolist() == [1.0]         # Paris winter, unix unchanged
    assert frame["time_lower_unix_utc_us"].tolist() == [
        int(dt.datetime(2025, 12, 6, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1_000_000)
    ]
