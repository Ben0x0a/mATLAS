"""Unit tests for the v3 transform registry, builtins, and timestamp codecs.

Used by:    pytest.
Depends on: model_atlas.transforms.registry/.builtin, presets.expr (pipe parser).
"""
from __future__ import annotations

import pytest

import model_atlas.transforms.builtin as builtin
from model_atlas.presets.expr import parse_pipe
from model_atlas.transforms.builtin import epoch_to_us, parse_datetime_to_us, tz_offset_to_hours
from model_atlas.transforms.registry import PipeContext, TransformHardError, run_pipe


def run(value, pipe_str, *, lookup_tables=None, patterns=None):
    warnings: list[str] = []
    ctx = PipeContext(lookup_tables=lookup_tables or {}, patterns=patterns or {})
    result = run_pipe(value, parse_pipe(pipe_str), ctx, warnings)
    return result, warnings


def test_cast_and_scale() -> None:
    assert run("10", "cast(float) | scale(3.6)")[0] == 36.0
    assert run("48.8566", "cast(float)")[0] == 48.8566
    assert run(42, "cast(str)")[0] == "42"


def test_arithmetic() -> None:
    value, warnings = run(5, "arithmetic((value + 1) * 2)")
    assert value == 12
    assert warnings == []


def test_lookup_by_name_and_policies() -> None:
    tables = {"prov": {1: "GNSS", 4: "WiFi"}}
    assert run(4, "lookup(prov)", lookup_tables=tables)[0] == "WiFi"
    assert run("4", "lookup(prov)", lookup_tables=tables)[0] == "WiFi"
    assert run(9, "lookup(prov, on_unknown=raw)", lookup_tables=tables)[0] == 9
    assert run(9, "lookup(prov, on_unknown=null)", lookup_tables=tables)[0] is None
    with pytest.raises(TransformHardError):
        run(9, "lookup(prov, on_unknown=error)", lookup_tables=tables)
    with pytest.raises(TransformHardError):
        run(1, "lookup(missing)", lookup_tables=tables)


def test_regex_named_group_only() -> None:
    patterns = {"coords": r"(?P<lat>-?\d+\.\d+) - (?P<lon>-?\d+\.\d+)"}
    assert run("38.4 - -0.4", "regex(coords, group=lat)", patterns=patterns)[0] == "38.4"
    assert run("38.4 - -0.4", "regex(coords, group=lon)", patterns=patterns)[0] == "-0.4"
    assert run("no match", "regex(coords, group=lat)", patterns=patterns)[0] is None
    with pytest.raises(TransformHardError):
        run("x", "regex(coords)", patterns=patterns)  # group is mandatory


def test_split_by_index() -> None:
    assert run("a,b,c", "split(',', index=1)")[0] == "b"


def test_none_passes_through() -> None:
    value, warnings = run(None, "arithmetic(value * 2) | cast(int)")
    assert value is None
    assert warnings == []


def test_on_error_policies() -> None:
    assert run("abc", "cast(int)")[0] is None              # default null
    assert run("abc", "cast(int, on_error=raw)")[0] == "abc"
    with pytest.raises(ValueError):
        run("abc", "cast(int, on_error=error)")


def test_unknown_transform_is_hard_error() -> None:
    with pytest.raises(TransformHardError):
        run(1, "bogus(x)")


def test_arithmetic_rejects_unsafe_expression() -> None:
    with pytest.raises(ValueError):
        run(1, "arithmetic(__import__('os').system('echo hi'), on_error=error)")


# --- timestamp codecs -------------------------------------------------------

def test_parse_datetime_axiom_format_to_unix_us() -> None:
    assert parse_datetime_to_us("06.12.2025 13:02:08.744", "%d.%m.%Y %H:%M:%S.%f") == 1765026128744000


def test_parse_datetime_honours_tz_offset() -> None:
    utc = parse_datetime_to_us("06.12.2025 13:00:00.000", "%d.%m.%Y %H:%M:%S.%f")
    plus_two = parse_datetime_to_us("06.12.2025 13:00:00.000", "%d.%m.%Y %H:%M:%S.%f", "UTC+02:00")
    assert utc - plus_two == 2 * 3600 * 1_000_000


@pytest.mark.parametrize("value,expected", [
    (None, 0.0), (2, 2.0), ("UTC", 0.0), ("Z", 0.0),
    ("UTC+02:00", 2.0), ("+02:00", 2.0), ("-0230", -2.5),
])
def test_tz_offset_to_hours(value, expected) -> None:
    assert tz_offset_to_hours(value) == expected


def test_tz_offset_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        tz_offset_to_hours("not-a-zone")


def test_epoch_codecs() -> None:
    assert epoch_to_us(1, "unix_s") == 1_000_000
    assert epoch_to_us(1000, "unix_ms") == 1_000_000
    # Cocoa epoch: seconds since 2001-01-01 -> +978307200 to reach the Unix epoch.
    assert epoch_to_us(0, "cocoa") == 978_307_200 * 1_000_000
    with pytest.raises(TransformHardError):
        epoch_to_us(1, "bogus")
