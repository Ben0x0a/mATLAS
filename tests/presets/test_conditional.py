"""Tests for preset conditional mapping values (``if / elif / else``).

Covers the expr parser (``parse_cond``), the resolver (``presets.resolve.resolve_cond``
+ ``cond_match``) — first-match-wins, elif chains, else fall-through, ``!=``, the
explicit ``NA`` missing test and ref comparands — and the spec-loading validation path.
The end-to-end assemble coverage lives in tests/ingest/test_assemble.py (the engine the
resolvers now feed).
Used by:    pytest.
Depends on: presets.expr, presets.spec, presets.fields, presets.lint, presets.resolve,
            pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_atlas.presets.expr import (
    NA_SENTINEL,
    Cond,
    CondBranch,
    Ref,
    is_condition,
    parse_cond,
)
from model_atlas.presets.fields import make_resolver
from model_atlas.presets.lint import lint_spec
from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.presets.resolve import cond_match as _cond_match
from model_atlas.presets.resolve import resolve_cond as _resolve_cond


class _Env:
    """The resolvers' duck-typed env: only ``.entity`` / ``.concerns`` are read."""

    entity = None
    concerns = None


_ENV = _Env()


def _resolve(text: str, row: dict[str, object]) -> object:
    """Parse ``text`` as a conditional and evaluate it against ``row``, returning the value."""
    cond = parse_cond(text)
    resolve = make_resolver(list(row.keys()))
    value, _source = _resolve_cond(cond, row, resolve, {}, _ENV, {})
    return value


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("t.yaml"))


# --- parsing (expr.parse_cond) ----------------------------------------------

def test_is_condition_detects_the_if_prefix() -> None:
    assert is_condition("if field(X) == \"NA\" then const(instant)")
    assert is_condition("  if field(X) == \"NA\" then const(instant)")
    assert not is_condition("field(X)")
    assert not is_condition("const(instant)")
    assert not is_condition(None)


def test_parse_single_branch_no_else() -> None:
    cond = parse_cond('if field(X) == "NA" then const(instant)')
    assert cond == Cond(
        branches=(CondBranch(Ref("field", "X"), "==", Ref("const", "NA"), Ref("const", "instant")),),
        else_=None,
    )


def test_parse_elif_chain_with_else() -> None:
    cond = parse_cond(
        'if field(X) == "NA" then const(instant) '
        'elif field(Y) == "1" then const(interval) '
        'else const(continuous)'
    )
    assert len(cond.branches) == 2
    assert cond.branches[0].then == Ref("const", "instant")
    assert cond.branches[1] == CondBranch(Ref("field", "Y"), "==", Ref("const", "1"), Ref("const", "interval"))
    assert cond.else_ == Ref("const", "continuous")


def test_parse_ref_comparand_and_bang_eq() -> None:
    # A comparand may itself be a ref (field-to-field compare); != is supported.
    cond = parse_cond("if field(A) != field(B) then const(diff) else const(same)")
    (branch,) = cond.branches
    assert branch.op == "!=" and branch.comparand == Ref("field", "B")


def test_cond_refs_lists_every_reference() -> None:
    cond = parse_cond('if field(X) == field(Y) then const(a) elif field(Z) == "1" then const(b) else const(c)')
    kinds_args = [(r.kind, r.arg) for r in cond.refs()]
    assert ("field", "X") in kinds_args and ("field", "Y") in kinds_args and ("field", "Z") in kinds_args


@pytest.mark.parametrize("text", [
    "field(X) == const(NA) then const(a)",     # missing leading 'if'
    "if field(X) then const(a)",               # no operator/comparand
    "if field(X) == const(NA) const(a)",       # missing 'then'
    "if field(X) ~ const(NA) then const(a)",   # unknown operator
    "if field(X) == const(NA) then const(a) leftover",  # trailing tokens
])
def test_malformed_conditional_raises(text: str) -> None:
    with pytest.raises(ValueError):
        parse_cond(text)


def test_typo_ref_kind_comparand_raises_at_parse() -> None:
    # A comparand that LOOKS like a call but names an unknown kind (``feild`` for ``field``)
    # must RAISE rather than silently become the unmatchable literal "feild(Y)" — which would
    # quietly send every row to the else-branch with no error.
    with pytest.raises(ValueError, match="reference kind"):
        parse_cond("if field(X) == feild(Y) then const(a) else const(b)")
    # a correctly-spelled ref comparand still parses
    assert isinstance(
        parse_cond("if field(X) == field(Y) then const(a) else const(b)"), Cond)


# --- evaluation (presets.resolve.resolve_cond / cond_match) ------------------

def test_na_test_true_when_missing_false_when_present() -> None:
    # field(X) == "NA" is the explicit missing test.
    assert _resolve('if field(X) == "NA" then const(instant)', {"X": None}) == "instant"
    # Present value, no else -> the sentinel (NOT None: the conditional ran, nothing matched).
    result = _resolve('if field(X) == "NA" then const(instant)', {"X": "here"})
    assert result is NA_SENTINEL
    assert result is not None


def test_bare_na_comparand_is_also_a_missing_test() -> None:
    # Unquoted NA behaves identically to "NA".
    assert _resolve("if field(X) == NA then const(instant)", {"X": None}) == "instant"


def test_elif_picks_the_middle_branch() -> None:
    text = (
        'if field(X) == "NA" then const(instant) '
        'elif field(Y) == "1" then const(interval) '
        'else const(continuous)'
    )
    assert _resolve(text, {"X": None, "Y": "9"}) == "instant"       # first branch
    assert _resolve(text, {"X": "p", "Y": "1"}) == "interval"       # middle (elif) branch
    assert _resolve(text, {"X": "p", "Y": "9"}) == "continuous"     # else fall-through


def test_bang_eq_and_ref_comparand() -> None:
    present = 'if field(X) != "NA" then const(present) else const(absent)'
    assert _resolve(present, {"X": "v"}) == "present"
    assert _resolve(present, {"X": None}) == "absent"
    compare = "if field(A) == field(B) then const(same) else const(diff)"
    assert _resolve(compare, {"A": "x", "B": "x"}) == "same"
    assert _resolve(compare, {"A": "x", "B": "y"}) == "diff"


def test_first_match_wins_on_overlapping_conditions() -> None:
    text = 'if field(X) == "1" then const(a) elif field(X) == "1" then const(b)'
    assert _resolve(text, {"X": "1"}) == "a"


def test_equality_tolerates_source_type_drift() -> None:
    # A numeric source cell (e.g. from sqlite) still matches a quoted-literal comparand.
    assert _cond_match(1, "==", Ref("const", "1"), "1") is True
    assert _cond_match("1", "==", Ref("const", "1"), "1") is True
    assert _cond_match(2, "==", Ref("const", "1"), "1") is False


def test_equality_tolerates_integral_float_drift() -> None:
    # A pandas/Excel numeric cell arrives as a float 1.0; it must still equal the integer
    # literal "1" (str(1.0) == "1.0" != "1", so a plain string fallback would wrongly fail).
    assert _cond_match(1.0, "==", Ref("const", "1"), "1") is True
    assert _cond_match(1.0, "!=", Ref("const", "1"), "1") is False
    assert _cond_match(2.0, "==", Ref("const", "1"), "1") is False
    # End-to-end through the resolver: a float source cell selects the matching branch.
    assert _resolve('if field(ZTYPE) == "1" then const(match) else const(other)',
                    {"ZTYPE": 1.0}) == "match"


# --- load-time validation over a conditional-bearing spec --------------------

_LINK_COND = """
preset: {id: t.cond, name: Cond, version: 1.0, tier: secondary}
input_selector: {format: csv, name: v.csv}
source_record_uid: field(ItemID)
common:
  entity: const(device)
  raw_source_path: const(/x)
  # A link field mapped by its full name (owner's decision-33 example) resolves through
  # _parse_field, so a conditional is accepted here — unlike the short `links:` block, which
  # stays bare-enum only.
  occupancy: if field(Created) == "NA" then const(once)
observations:
  - position:
      latitude: field(Lat)
      longitude: field(Lon)
      position_source: if field(Kind) == "cell" then const(cell) else const(GNSS)
    time:
      instant: field(Created)
    links:
      spatial: at
      entity: self
"""


def test_conditional_enum_result_is_validated_at_load() -> None:
    # A conditional producing an invalid controlled-vocabulary value must fail at parse time,
    # exactly as a plain const would.
    bad = _LINK_COND.replace("then const(once)", "then const(not_a_link)")
    with pytest.raises(ValueError):
        _preset(bad)


def test_conditional_referencing_undeclared_field_lint_warns_not_crashes() -> None:
    # A conditional that reads a field not in expected_fields should produce the ordinary
    # "mapped-not-declared" warning rather than crashing the linter.
    text = _LINK_COND.replace(
        "input_selector: {format: csv, name: v.csv}",
        "input_selector: {format: csv, name: v.csv}\nexpected_fields: [Lat, Lon, Created, ItemID]",
    )
    findings = lint_spec(_preset(text))
    assert any(f.code == "mapped-not-declared" and "Kind" in f.message for f in findings)
