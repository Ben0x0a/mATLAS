"""Unit tests for the preset pipe language (presets.pipes): registry, runner, builtins.

Moved from tests/transforms/test_transforms.py at the T60 cut-over — the pipe registry
and its built-in transforms folded into ``model_atlas.presets.pipes``; the retired
epoch/strptime codecs' behaviour is covered by tests/integrate/test_temporal.py.
Used by:    pytest.
Depends on: model_atlas.presets.pipes, presets.expr (pipe parser).
"""
from __future__ import annotations

import pytest

from model_atlas.presets.expr import parse_pipe
from model_atlas.presets.pipes import PipeContext, TransformHardError, run_pipe


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
