"""Regression: zone_offset_hours_at must handle out-of-range instants on every platform.

datetime.fromtimestamp is CRT-backed on Windows and raises OSError [Errno 22] for pre-1970 or
far-future timestamps that occur in real forensic data; the epoch + timedelta implementation is
pure Python and valid across the whole datetime range.
"""
from __future__ import annotations

from model_atlas.transforms.builtin import zone_offset_hours_at

_US_PER_YEAR = 365 * 24 * 3600 * 1_000_000


def test_negative_instant_does_not_raise() -> None:
    # 1960 (pre-Unix-epoch) — Windows fromtimestamp rejects this; arithmetic must not.
    offset = zone_offset_hours_at("Europe/Paris", -10 * _US_PER_YEAR)
    assert offset == 1.0                          # Paris standard offset (no DST in winter 1960)


def test_far_future_instant_does_not_raise() -> None:
    offset = zone_offset_hours_at("Europe/Paris", 200 * _US_PER_YEAR)   # ~2170
    assert offset in (1.0, 2.0)                    # a valid Paris offset, no OSError


def test_normal_instant_summer_offset() -> None:
    # 2025-07-01 ~ +2 (CEST). Sanity that the arithmetic path matches expected DST behaviour.
    summer_2025_us = int((55 * _US_PER_YEAR) + (182 * 24 * 3600 * 1_000_000))
    assert zone_offset_hours_at("Europe/Paris", summer_2025_us) == 2.0
