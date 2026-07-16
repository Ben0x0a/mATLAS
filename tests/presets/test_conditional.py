"""Tests for preset conditional mapping values (decision 33: ``if / elif / else``).

Covers the expr parser (``parse_cond``), the assemble-time evaluator (``_resolve_cond`` +
``_cond_match``) — first-match-wins, elif chains, else fall-through, ``!=``, the explicit
``NA`` missing test and ref comparands — and the end-to-end spec-loading + assemble path,
pinning the crucial invariant that the ``NA_SENTINEL`` (conditional matched nothing) is
DISTINGUISHABLE from None (field never mapped).
Used by:    pytest.
Depends on: presets.expr, presets.spec, presets.fields, presets.lint, transforms.assemble,
            tests.support, pandas, pyyaml.
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
from model_atlas.transforms.assemble import BuildEnv, _cond_match, _resolve_cond
from tests.support import build_rows

_ENV = BuildEnv(
    input_file_path="/dev/v.csv", input_file_name="v.csv", source_fingerprint="fp",
    source_file_path="/dev/file", source_tier="secondary",
)


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


# --- evaluation (assemble._resolve_cond / _cond_match) ----------------------

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


# --- end-to-end through spec loading + assemble -----------------------------

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
  spatial_temporal_link: if field(Created) == "NA" then const(instant)
assertions:
  - position:
      latitude_wgs84: field(Lat)
      longitude_wgs84: field(Lon)
      position_type: if field(Kind) == "cell" then const(tower) else const(gps)
    time:
      instant: field(Created)
    links:
      entity_position: at
      entity_time: recorded_at
"""


def test_end_to_end_conditional_link_and_non_link_column() -> None:
    records = [
        {"Lat": "1.5", "Lon": "2.5", "Created": "150", "Kind": "cell", "ItemID": "A1"},
        {"Lat": "1.5", "Lon": "2.5", "Created": None, "Kind": "gps", "ItemID": "A2"},
    ]
    frame, warnings = build_rows(records, _preset(_LINK_COND), _ENV)
    assert warnings == []
    row_present, row_missing = frame.iloc[0], frame.iloc[1]

    # Non-link column (position_type) resolves through the very same conditional path.
    assert row_present["position_type"] == "tower"   # Kind == "cell" -> then branch
    assert row_missing["position_type"] == "gps"     # else branch

    # Link column: Created present + no else -> the sentinel; Created NA -> the then branch.
    # Through the store round-trip the sentinel keeps its VALUE (the "N/A" text NA_SENTINEL
    # equals) though not its Python type — still unmistakably distinct from a NULL.
    assert row_present["spatial_temporal_link"] == NA_SENTINEL == "N/A"
    assert row_missing["spatial_temporal_link"] == "instant"

    # The crucial distinction: the sentinel (conditional matched nothing) is NOT None, and an
    # unmapped model field (altitude_m — never mentioned) IS None. They are distinguishable.
    assert row_present["spatial_temporal_link"] is not None
    assert row_present["altitude_m"] is None


def test_conditional_enum_result_is_validated_at_load() -> None:
    # A conditional producing an invalid controlled-vocabulary value must fail at parse time,
    # exactly as a plain const would.
    bad = _LINK_COND.replace("then const(instant)", "then const(not_a_link)")
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
