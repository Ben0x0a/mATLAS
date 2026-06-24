"""End-to-end: a temporal spec's captured utc_offset_hours is applied during parsing (v3).

Used by:    pytest.
Depends on: pipeline.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from model_atlas.pipeline import process

_PRESET = """
preset: {id: t.tz, name: TZ, version: 1.0, tier: secondary}
input_selector: {format: csv, name: tz.csv}
patterns: {tz: "(?P<z>UTC[+-]\\\\d{2}:\\\\d{2})"}
source_record_uid: column(Item)
assertions:
  - position: {latitude_wgs84: column(Lat)}
    time:
      instant: 'column("Timestamp - * ")'
      format: "%Y-%m-%d %H:%M:%S"
      zone: { from: 'header("Timestamp - * ")', pipe: "regex(tz, group=z)" }
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""

_CSV = "Lat,Timestamp - UTC+02:00 ,Item\n1.5,2025-06-06 13:00:00,A\n"


def test_captured_timezone_is_applied_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "tz.csv").write_text(_CSV, encoding="utf-8")
    (tmp_path / "p.yaml").write_text(_PRESET, encoding="utf-8")
    out = tmp_path / "out.csv"

    process(tmp_path / "tz.csv", tmp_path / "p.yaml", out, linked_entity="subject")
    row = next(csv.DictReader(out.open(encoding="utf-8-sig")))

    # 13:00 at the captured +02:00 == 11:00:00 UTC.
    expected = int(dt.datetime(2025, 6, 6, 11, 0, 0, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000
    assert int(row["time_lower_unix_utc_us"]) == expected
    assert row["utc_offset_hours"] == "2.0"                   # CSV cell of the signed-hours float (+02:00)
