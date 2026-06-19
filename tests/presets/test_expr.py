"""Exhaustive tests for the v3 reference + pipe expression parser (presets.expr).

Covers every reference kind and pipe shape, plus the malformed inputs that MUST be
rejected, so the parser's contract is pinned and regressions surface immediately.
Used by:    pytest.
Depends on: model_atlas.presets.expr.
"""
from __future__ import annotations

import pytest

from model_atlas.presets.expr import Ref, parse_pipe, parse_ref


# --- references: valid ------------------------------------------------------

def test_column_plain_and_glob_and_quoted() -> None:
    assert parse_ref("column(ZLAT)") == Ref("column", "ZLAT")
    assert parse_ref('column("Altitude (m)")') == Ref("column", "Altitude (m)")
    assert parse_ref('column("Timestamp - * (dd.MM.yyyy)")').arg == "Timestamp - * (dd.MM.yyyy)"


def test_header_filename_param() -> None:
    assert parse_ref('header("TS - *")') == Ref("header", "TS - *")
    assert parse_ref("filename(name)") == Ref("filename", "name")
    assert parse_ref("filename(stem)").arg == "stem"
    assert parse_ref("filename(path)").arg == "path"
    assert parse_ref("param(entity)") == Ref("param", "entity")
    assert parse_ref("param(linked_entity)").arg == "linked_entity"


@pytest.mark.parametrize("text,expected", [
    ("const(device)", "device"),
    ("const(42)", 42),
    ("const(3.5)", 3.5),
    ("const(true)", True),
    ("const(false)", False),
    ("const(null)", None),
    ('const("a literal string")', "a literal string"),
    ('const("12345")', "12345"),   # quoted digits stay a string
])
def test_const_coercion(text, expected) -> None:
    assert parse_ref(text).arg == expected


def test_whitespace_tolerated() -> None:
    assert parse_ref("  column( ZLAT ) ").arg == "ZLAT"


# --- references: invalid (must raise) ---------------------------------------

@pytest.mark.parametrize("text", [
    "device",                 # bare scalar is not a call
    "ZLAT",
    "column",                 # missing parens
    "columnZLAT",
    "unknownkind(x)",         # not a known ref kind
    "filename(bogus)",        # bad filename token
    "param(nope)",            # bad param token
    "column()",               # empty column name
    "header()",               # empty header glob
])
def test_invalid_reference_raises(text) -> None:
    with pytest.raises(ValueError):
        parse_ref(text)


def test_non_string_reference_raises() -> None:
    with pytest.raises(ValueError):
        parse_ref(42)  # type: ignore[arg-type]


# --- pipes: valid -----------------------------------------------------------

def test_single_and_chained_steps() -> None:
    steps = parse_pipe("cast(int)")
    assert len(steps) == 1 and steps[0].name == "cast" and steps[0].args == ("int",)
    chained = parse_pipe("cast(float) | scale(3.6)")
    assert [s.name for s in chained] == ["cast", "scale"]
    assert chained[1].args == (3.6,)


def test_kwargs_and_positional() -> None:
    (step,) = parse_pipe("lookup(recovery, on_unknown=null)")
    assert step.name == "lookup" and step.args == ("recovery",) and step.kwargs == {"on_unknown": None}
    (regex,) = parse_pipe("regex(coords, group=lat)")
    assert regex.kwargs == {"group": "lat"}


def test_quoted_arg_protects_separators() -> None:
    # The separator is a literal comma inside quotes; the top-level comma splits args.
    (step,) = parse_pipe('split(",", index=0)')
    assert step.args == (",",) and step.kwargs == {"index": 0}
    # A pipe character inside quotes must not split the chain.
    (only,) = parse_pipe("regex(p, group='a|b')")
    assert only.kwargs == {"group": "a|b"}


def test_bare_step_name_and_empty_pipe() -> None:
    (step,) = parse_pipe("cast")
    assert step.name == "cast" and step.args == () and step.kwargs == {}
    assert parse_pipe(None) == ()
    assert parse_pipe("") == ()
    assert parse_pipe("  |  ") == ()  # empty segments are skipped


def test_on_error_is_carried_as_kwarg() -> None:
    (step,) = parse_pipe("cast(int, on_error=raw)")
    assert step.kwargs == {"on_error": "raw"}


# --- pipes: invalid ---------------------------------------------------------

def test_pipe_step_must_be_a_call() -> None:
    with pytest.raises(ValueError):
        parse_pipe("cast(int) | 3.6")   # second step is not name(args)


def test_non_string_pipe_raises() -> None:
    with pytest.raises(ValueError):
        parse_pipe(123)  # type: ignore[arg-type]
