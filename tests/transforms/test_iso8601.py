"""Tests for the iso8601 time codec (Cellebrite TimeStamp: zone-qualified + naive)."""
from __future__ import annotations

import datetime as dt

from model_atlas.presets.expr import Ref
from model_atlas.presets.spec import TimeSpec
from model_atlas.transforms.assemble import _decode_time
from model_atlas.transforms.builtin import datetime_to_us, parse_iso8601, parse_zone_token

_SPEC = TimeSpec(kind="instant", lower=Ref(kind="column", arg="t"),
                 upper=Ref(kind="column", arg="t"), format="iso8601")


def test_parse_iso8601_variants() -> None:
    assert parse_iso8601("2025-12-05T10:19:15.073+00:00").tzinfo is not None
    assert parse_iso8601("2026-06-15T05:00:38.784").tzinfo is None  # naive
    assert parse_iso8601("") is None
    assert parse_iso8601(None) is None
    assert parse_iso8601("not a date") is None


def test_zone_qualified_keeps_offset_and_ms() -> None:
    us, off = _decode_time("2025-12-05T10:19:15.073+00:00", _SPEC, None, None, [])
    assert off == 0.0
    # Millisecond precision preserved (…15.073 -> .073000 us).
    assert dt.datetime.fromtimestamp(us / 1e6, dt.timezone.utc).isoformat() == \
        "2025-12-05T10:19:15.073000+00:00"


def test_naive_resolved_in_local_zone() -> None:
    warnings: list[str] = []
    us, off = _decode_time(
        "2026-06-15T05:00:38.784", _SPEC, parse_zone_token("[DST]"), "Europe/Paris", warnings
    )
    assert off == 2.0  # Paris is UTC+2 in June (DST)
    assert dt.datetime.fromtimestamp(us / 1e6, dt.timezone.utc).isoformat() == \
        "2026-06-15T03:00:38.784000+00:00"


def test_empty_timestamp_is_none() -> None:
    assert _decode_time("", _SPEC, None, None, []) == (None, None)


def test_naive_without_zone_flags_offset_unknown() -> None:
    from model_atlas.model.families import UTC_OFFSET_UNKNOWN

    # No zone in the value and none configured: the instant is materialised (assumed UTC)
    # but the offset is flagged unknown, not reported as a misleading 0.0 or null.
    us, off = _decode_time("2026-06-15T05:00:38.784", _SPEC, None, None, [])
    assert us is not None
    assert off == UTC_OFFSET_UNKNOWN
    # A zone-qualified value still gets a real, known offset.
    _us2, off2 = _decode_time("2026-06-15T05:00:38.784+00:00", _SPEC, None, None, [])
    assert off2 == 0.0
