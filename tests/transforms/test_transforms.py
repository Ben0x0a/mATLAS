"""Unit tests for the transform registry and builtin library.

Defines:    tests for arithmetic/cast/lookup/regex_extract/split, pipe composition,
            the on_error policy, and None passthrough.
Used by:    pytest.
Depends on: model_atlas.transforms.registry and .builtin (imported to register).
"""
from __future__ import annotations

import pytest

import model_atlas.transforms.builtin  # noqa: F401 - registers the builtins
from model_atlas.transforms.registry import apply_pipe


def test_arithmetic_then_cast_int() -> None:
    value, warnings = apply_pipe(
        694223890,
        [{"arithmetic": "(value + 978307200) * 1000"}, {"cast": "int"}],
    )
    assert value == int((694223890 + 978307200) * 1000)
    assert warnings == []


def test_speed_m_per_s_to_kmh() -> None:
    value, _ = apply_pipe("10", [{"cast": "float"}, {"arithmetic": "value * 3.6"}])
    assert value == 36.0


def test_cast_float_and_str() -> None:
    assert apply_pipe("48.8566", [{"cast": "float"}])[0] == 48.8566
    assert apply_pipe(42, [{"cast": "str"}])[0] == "42"


def test_lookup_matches_int_and_string_keys() -> None:
    table = {1: "GNSS", 4: "WiFi", 6: "LTE"}
    assert apply_pipe(4, [{"lookup": table}])[0] == "WiFi"
    assert apply_pipe("4", [{"lookup": table}])[0] == "WiFi"


def test_lookup_on_unknown_policies() -> None:
    table = {1: "GNSS"}
    assert apply_pipe(9, [{"lookup": table, "on_unknown": "raw"}])[0] == 9
    assert apply_pipe(9, [{"lookup": table, "on_unknown": "null"}])[0] is None
    with pytest.raises(ValueError):
        apply_pipe(9, [{"lookup": table, "on_unknown": "error"}])


def test_regex_extract_capture_group() -> None:
    assert apply_pipe("rowid=4821", [{"regex_extract": r"rowid=(\d+)"}])[0] == "4821"
    # No match yields None, not an error.
    assert apply_pipe("nope", [{"regex_extract": r"rowid=(\d+)"}])[0] is None


def test_split_by_index() -> None:
    assert apply_pipe("a,b,c", [{"split": ",", "index": 1}])[0] == "b"


def test_parse_datetime_axiom_format_to_unix_ns() -> None:
    # Matches the real AXIOM "Cached Locations" value (in nanoseconds).
    value, warnings = apply_pipe(
        "06.12.2025 13:02:08.744",
        [{"parse_datetime": "%d.%m.%Y %H:%M:%S.%f"}],
    )
    assert value == 1765026128744000000
    assert warnings == []


def test_parse_datetime_honours_tz_offset() -> None:
    utc, _ = apply_pipe("06.12.2025 13:00:00.000", [{"parse_datetime": "%d.%m.%Y %H:%M:%S.%f"}])
    plus_two, _ = apply_pipe(
        "06.12.2025 13:00:00.000",
        [{"parse_datetime": "%d.%m.%Y %H:%M:%S.%f", "tz_offset_hours": 2}],
    )
    # +02:00 means the same wall-clock is two hours earlier in UTC (in nanoseconds).
    assert utc - plus_two == 2 * 3600 * 1_000_000_000


def test_parse_datetime_bad_value_follows_on_error() -> None:
    value, warnings = apply_pipe("not a date", [{"parse_datetime": "%d.%m.%Y"}])
    assert value is None
    assert len(warnings) == 1


def test_none_passes_through_every_step() -> None:
    value, warnings = apply_pipe(None, [{"arithmetic": "value * 2"}, {"cast": "int"}])
    assert value is None
    assert warnings == []


def test_on_error_null_records_warning_and_drops() -> None:
    value, warnings = apply_pipe("abc", [{"cast": "int"}])
    assert value is None
    assert len(warnings) == 1


def test_on_error_raw_keeps_input() -> None:
    value, warnings = apply_pipe("abc", [{"cast": "int", "on_error": "raw"}])
    assert value == "abc"
    assert len(warnings) == 1


def test_on_error_error_raises() -> None:
    with pytest.raises(ValueError):
        apply_pipe("abc", [{"cast": "int", "on_error": "error"}])


def test_step_must_name_exactly_one_transform() -> None:
    with pytest.raises(ValueError):
        apply_pipe(1, [{"cast": "int", "arithmetic": "value"}])
    with pytest.raises(ValueError):
        apply_pipe(1, [{"unknown_transform": "x"}])


def test_arithmetic_rejects_unsafe_expression() -> None:
    with pytest.raises(ValueError):
        apply_pipe(1, [{"arithmetic": "__import__('os').system('echo hi')", "on_error": "error"}])
