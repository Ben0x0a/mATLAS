"""Tests for timezone-driven datetime parsing (roadmap 1f).

Covers the tz-offset string parser, parse_datetime with a string offset, and the
end-to-end auto-application of a captured ``time_zone`` to the conversion.

Used by:    pytest.
Depends on: transforms.builtin, pipeline.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from model_atlas.pipeline import process
from model_atlas.transforms.builtin import parse_datetime, tz_offset_to_hours


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 0.0),
        (2, 2.0),
        (-5.5, -5.5),
        ("UTC", 0.0),
        ("Z", 0.0),
        ("", 0.0),
        ("UTC+02:00", 2.0),
        ("+02:00", 2.0),
        ("-0230", -2.5),
        ("UTC-05:00", -5.0),
        ("+05", 5.0),
    ],
)
def test_tz_offset_to_hours(value, expected) -> None:
    assert tz_offset_to_hours(value) == expected


def test_tz_offset_to_hours_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        tz_offset_to_hours("not-a-zone")


def test_parse_datetime_accepts_string_offset() -> None:
    utc = parse_datetime("2025-06-06 13:00:00", format="%Y-%m-%d %H:%M:%S")
    plus2 = parse_datetime("2025-06-06 13:00:00", format="%Y-%m-%d %H:%M:%S", tz_offset_hours="UTC+02:00")
    # 13:00 at +02:00 is 11:00 UTC, i.e. two hours (in ns) earlier than the UTC reading.
    assert utc - plus2 == 2 * 3600 * 1_000_000_000


_PRESET_TZ_HEADER = """
name: TZ
parser: {name: tz, version: "1.0"}
selectors: [{source_type: csv, file_name: "tz.csv"}]
source_row_id: {from: Item}
assertions:
  - latitude_wgs84: {from: Lat, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - instant: "Timestamp - *"
        pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
        time_zone:
          from_name: "Timestamp - *"
          pipe: [{regex_extract: "(UTC[+-][0-9]{2}:[0-9]{2})"}]
"""

_CSV_TZ = "Lat,Timestamp - UTC+02:00,Item\n1.5,2025-06-06 13:00:00,A\n"


def test_captured_timezone_is_applied_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "tz.csv"
    source.write_text(_CSV_TZ, encoding="utf-8")
    preset = tmp_path / "p.yaml"
    preset.write_text(_PRESET_TZ_HEADER, encoding="utf-8")
    output = tmp_path / "out.csv"

    process(source, preset, output, linked_entity="subject")
    with output.open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))

    # 13:00 at the captured +02:00 == 11:00:00 UTC.
    expected = int(dt.datetime(2025, 6, 6, 11, 0, 0, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000
    assert int(row["time_lower_unix_ns"]) == expected
    assert row["time_zone"] == "UTC+02:00"
