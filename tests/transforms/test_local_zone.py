"""The easy DST case: an absolute-UTC (epoch) source records the configured local zone's
DST-aware offset per row, while the canonical unix stays UTC."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.transforms.assemble import BuildEnv, build_rows

_COCOA_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)


def _cocoa(year: int, month: int, day: int) -> float:
    """Cocoa seconds (since 2001-01-01 UTC) for 12:00 UTC on a date."""
    instant = dt.datetime(year, month, day, 12, 0, tzinfo=dt.timezone.utc)
    return (instant - _COCOA_EPOCH).total_seconds()


_PRESET = """
preset: {id: t.loc, name: Loc, version: 1.0, tier: primary}
input_selector: {format: sqlite, path: /x/db.sqlite, table: T}
common: {entity: const(device)}
assertions:
  - position: {latitude_wgs84: column(lat), longitude_wgs84: column(lon)}
    time: {instant: column(ts), epoch: cocoa}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def _rows(records: list[dict], local_zone: str | None):
    spec = preset_spec_from_yaml(yaml.safe_load(_PRESET), Path("t.yaml"))
    env = BuildEnv(linked_entity="subj", local_zone=local_zone)
    frame, _ = build_rows(records, spec, env, columns=["lat", "lon", "ts"])
    return frame


def test_local_zone_records_dst_aware_offset_for_epoch_source() -> None:
    winter = {"lat": "1.0", "lon": "2.0", "ts": _cocoa(2025, 1, 15)}  # Paris +1
    summer = {"lat": "3.0", "lon": "4.0", "ts": _cocoa(2025, 7, 15)}  # Paris +2
    frame = _rows([winter, summer], "Europe/Paris")
    assert list(frame["utc_offset_hours"]) == [1.0, 2.0]   # exact DST-aware offset per instant

    # The canonical unix is absolute UTC — identical whether or not a local zone is set.
    frame_utc = _rows([winter, summer], None)
    assert list(frame["time_lower_unix_utc_us"]) == list(frame_utc["time_lower_unix_utc_us"])


def test_no_local_zone_leaves_offset_null() -> None:
    frame = _rows([{"lat": "1.0", "lon": "2.0", "ts": _cocoa(2025, 7, 15)}], None)
    assert frame["utc_offset_hours"].isna().all()          # absence => unknown zone


def test_half_hour_zone_offset() -> None:
    frame = _rows([{"lat": "1.0", "lon": "2.0", "ts": _cocoa(2025, 7, 15)}], "Asia/Kolkata")
    assert list(frame["utc_offset_hours"]) == [5.5]        # India +05:30, no DST
