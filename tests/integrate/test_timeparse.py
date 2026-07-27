"""Unit tests for the pure zone/ISO primitives (integrate.timeparse).

Moved/condensed from tests/transforms/test_iso8601.py + test_transforms.py at the
cut-over to the relational engine — the primitives folded into
``model_atlas.integrate.timeparse``; the decode
LADDER that consumes them is covered by tests/integrate/test_temporal.py.
Used by:    pytest.
Depends on: model_atlas.integrate.timeparse.
"""
from __future__ import annotations

import pytest

from model_atlas.integrate.timeparse import (
    parse_iso8601,
    parse_zone_token,
    tz_offset_to_hours,
    zone_standard_offset_hours,
)


def test_parse_iso8601_variants() -> None:
    assert parse_iso8601("2025-12-05T10:19:15.073+00:00").tzinfo is not None
    assert parse_iso8601("2026-06-15T05:00:38.784").tzinfo is None  # naive
    assert parse_iso8601("") is None
    assert parse_iso8601(None) is None
    assert parse_iso8601("not a date") is None


@pytest.mark.parametrize("value,expected", [
    (None, 0.0), (2, 2.0), ("UTC", 0.0), ("Z", 0.0),
    ("UTC+02:00", 2.0), ("+02:00", 2.0), ("-0230", -2.5),
])
def test_tz_offset_to_hours(value, expected) -> None:
    assert tz_offset_to_hours(value) == expected


def test_tz_offset_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        tz_offset_to_hours("not-a-zone")


def test_parse_zone_token_variants() -> None:
    fixed = parse_zone_token("UTC+01:00")
    assert fixed.base_offset_hours == 1.0 and fixed.dst is False
    dst = parse_zone_token("UTC+01:00[DST]")
    assert dst.base_offset_hours == 1.0 and dst.dst is True
    local = parse_zone_token("local")
    assert local.base_offset_hours is None and local.dst is True
    assert parse_zone_token(None) is None
    assert parse_zone_token("") is None
    # Unparseable text keeps the raw token but claims no base offset.
    garbage = parse_zone_token("somewhere")
    assert garbage.base_offset_hours is None and garbage.raw == "somewhere"


def test_zone_standard_offset_hours_is_the_non_dst_offset() -> None:
    assert zone_standard_offset_hours("Europe/Paris") == 1.0
