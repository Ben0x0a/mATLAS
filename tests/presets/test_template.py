"""Tests for preset template mapping values (decision 42: ``format(...)``).

Covers the expr parser (``parse_template`` via ``parse_ref``) — literal/placeholder
segmentation, ``{{``/``}}`` escapes, malformed templates — the assemble-time renderer
(mixed render, a missing placeholder rendering empty, EVERY placeholder missing yielding
the same ``NA_SENTINEL`` identity as decision 33), a template as a conditional branch
result, and the end-to-end spec-loading + assemble path.
Used by:    pytest.
Depends on: presets.expr, presets.spec, presets.fields, presets.lint, transforms.assemble,
            tests.support, pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_atlas.presets.expr import (
    NA_SENTINEL,
    Ref,
    Template,
    flatten_ref,
    parse_cond,
    parse_ref,
)
from model_atlas.presets.fields import make_resolver
from model_atlas.presets.lint import lint_spec
from model_atlas.presets.spec import preset_spec_from_yaml
from model_atlas.transforms.assemble import BuildEnv, _resolve_cond, _resolve_ref
from tests.support import build_rows

_ENV = BuildEnv(
    input_file_path="/dev/v.csv", input_file_name="v.csv", source_fingerprint="fp",
    source_file_path="/dev/file", source_tier="secondary",
)


def _render(text: str, row: dict[str, object]) -> object:
    """Parse ``text`` as a single ref (a format template) and resolve it against ``row``."""
    ref = parse_ref(text)
    resolve = make_resolver(list(row.keys()))
    value, _source = _resolve_ref(ref, row, resolve, {}, _ENV, {})
    return value


def _preset(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("t.yaml"))


# --- parsing ------------------------------------------------------------------

def test_parse_template_segments() -> None:
    ref = parse_ref('format("{field(FirstName)} {field(LastName)} from {field(City)}")')
    assert ref.kind == "format" and isinstance(ref.arg, Template)
    assert ref.arg.segments == (
        Ref("field", "FirstName"), " ", Ref("field", "LastName"), " from ", Ref("field", "City"),
    )


def test_parse_template_escaped_braces_are_literal() -> None:
    ref = parse_ref('format("{{x}} = {field(A)}")')
    assert ref.arg.segments == ("{x} = ", Ref("field", "A"))


def test_template_refs_and_flatten() -> None:
    ref = parse_ref('format("{field(A)}-{const(k)}")')
    assert ref.arg.refs() == (Ref("field", "A"), Ref("const", "k"))
    assert flatten_ref(ref) == (Ref("field", "A"), Ref("const", "k"))
    # A non-template ref flattens to itself.
    assert flatten_ref(Ref("field", "X")) == (Ref("field", "X"),)


@pytest.mark.parametrize("text", [
    'format("{field(A)")',        # unbalanced '{'
    'format("field(A)}")',        # unbalanced '}'
    'format("{}")',               # empty placeholder
    'format("{bogus(A)}")',       # unknown ref kind inside a placeholder
    'format("")',                 # empty template
])
def test_malformed_template_raises(text: str) -> None:
    with pytest.raises(ValueError):
        parse_ref(text)


# --- rendering (assemble._resolve_template via _resolve_ref) -------------------

def test_mixed_literals_and_refs_render() -> None:
    row = {"FirstName": "Ada", "LastName": "Lovelace", "City": "London"}
    value = _render('format("{field(FirstName)} {field(LastName)} from {field(City)}")', row)
    assert value == "Ada Lovelace from London"


def test_missing_placeholder_renders_empty_in_place() -> None:
    row = {"FirstName": "Ada", "LastName": None, "City": "London"}
    value = _render('format("{field(FirstName)} {field(LastName)} from {field(City)}")', row)
    assert value == "Ada  from London"   # the missing name collapses; connectors stay


def test_all_placeholders_missing_yields_the_sentinel_identity() -> None:
    row = {"FirstName": None, "LastName": None, "City": None}
    value = _render('format("{field(FirstName)} {field(LastName)} from {field(City)}")', row)
    # The SAME sentinel object as decision 33 — identity, not just equal text.
    assert value is NA_SENTINEL
    assert value is not None


def test_non_field_placeholders_and_escapes_render() -> None:
    # const placeholders always count as present, and {{ }} render as literal braces.
    assert _render('format("{{v}} {const(1)}-{field(A)}")', {"A": "x"}) == "{v} 1-x"


def test_template_as_a_conditional_branch_result() -> None:
    # D42 composes with D33: `... then format(...)`.
    cond = parse_cond('if field(Kind) == "cell" then format("tower {field(CellID)}") else const(gps)')
    resolve = make_resolver(["Kind", "CellID"])
    value, _ = _resolve_cond(cond, {"Kind": "cell", "CellID": "42"}, resolve, {}, _ENV, {})
    assert value == "tower 42"
    value, _ = _resolve_cond(cond, {"Kind": "sat", "CellID": "42"}, resolve, {}, _ENV, {})
    assert value == "gps"


# --- end-to-end through spec loading + assemble --------------------------------

_TEMPLATE_PRESET = """
preset: {id: t.tmpl, name: Tmpl, version: 1.0, tier: secondary}
input_selector: {format: csv, name: v.csv}
source_record_uid: field(ItemID)
common:
  entity: const(device)
  raw_source_path: const(/x)
  source_label: format("{field(First)} {field(Last)}")
assertions:
  - position:
      latitude_wgs84: field(Lat)
      longitude_wgs84: field(Lon)
    time:
      instant: field(Created)
    links: {entity_position: at, entity_time: recorded_at, spatial_temporal: instant}
"""


def test_end_to_end_template_column() -> None:
    records = [
        {"Lat": "1.5", "Lon": "2.5", "Created": "150", "First": "Ada", "Last": "Lovelace", "ItemID": "A1"},
        {"Lat": "1.5", "Lon": "2.5", "Created": "160", "First": None, "Last": None, "ItemID": "A2"},
    ]
    frame, warnings = build_rows(records, _preset(_TEMPLATE_PRESET), _ENV)
    assert warnings == []
    both_present, both_missing = frame.iloc[0], frame.iloc[1]
    assert both_present["source_label"] == "Ada Lovelace"
    # All placeholders missing -> the sentinel value ("N/A" text after the store round-trip),
    # distinct from an unmapped field's None.
    assert both_missing["source_label"] == NA_SENTINEL == "N/A"
    assert both_missing["source_label"] is not None


def test_template_field_undeclared_lint_warns_not_crashes() -> None:
    # A template placeholder reading a field absent from expected_fields must surface the
    # ordinary "mapped-not-declared" warning rather than crash the linter.
    text = _TEMPLATE_PRESET.replace(
        "input_selector: {format: csv, name: v.csv}",
        "input_selector: {format: csv, name: v.csv}\n"
        "expected_fields: [Lat, Lon, Created, First, ItemID]",
    )
    findings = lint_spec(_preset(text))
    assert any(f.code == "mapped-not-declared" and "Last" in f.message for f in findings)
