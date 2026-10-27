"""Tests for the preset linter (presets.lint).

Covers the ERROR path (unparseable presets) and each WARNING/ADVICE check, plus a
clean preset producing no errors/warnings.
Used by:    pytest.
Depends on: model_atlas.presets.lint, pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from model_atlas.presets.lint import ADVICE, ERROR, WARNING, lint_file, lint_paths, lint_spec
from model_atlas.presets.spec import preset_spec_from_yaml

_CLEAN = """
preset: {id: ios.demo.clean, name: Clean, os: iOS, tool: AXIOM, os_version: ">=15", version: 1.0, tier: secondary}
input_selector: {format: csv, name: c.csv}
expected_fields: [Lat, Lon, TS, Loc, "Item ID", Source]
source_record_uid: 'field("Item ID")'
common: {entity: const(device), input_record_id: field(Loc), raw_source_path: field(Source)}
observations:
  - position: {latitude: field(Lat), longitude: field(Lon), spatial_referential: const(epsg:4326)}
    time: {instant: field(TS), epoch: unix_s}
    links: {entity: subject, spatial: measured, temporal: at_least_once}
"""


def _spec(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("t.yaml"))


def _codes(findings, severity=None):
    return {f.code for f in findings if severity is None or f.severity == severity}


def test_clean_preset_has_no_errors_or_warnings() -> None:
    findings = lint_spec(_spec(_CLEAN))
    assert _codes(findings, ERROR) == set()
    assert _codes(findings, WARNING) == set()


def test_unparseable_preset_yields_one_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("preset: {id: x.y, name: N, version: 1.0}\ninput_selector: {format: parquet, name: x.csv}\n", encoding="utf-8")
    findings = lint_file(bad)
    assert [f.severity for f in findings] == [ERROR]
    assert findings[0].code == "parse-error"


def test_rowid_as_uid_warns() -> None:
    text = _CLEAN.replace("source_record_uid: 'field(\"Item ID\")'", "source_record_uid: field(Z_PK)")
    assert "rowid-as-uid" in _codes(lint_spec(_spec(text)), WARNING)


def test_positionless_template_needs_no_spatial_link_and_must_not_carry_one() -> None:
    """Decision 55: no geometry, no position whose use a spatial link could describe."""
    no_position = _CLEAN.replace(
        "  - position: {latitude: field(Lat), longitude: field(Lon), spatial_referential: const(epsg:4326)}\n",
        "  - position:\n")
    without_link = no_position.replace(
        "{entity: subject, spatial: measured, temporal: at_least_once}",
        "{entity: subject, temporal: at_least_once}")
    assert _codes(lint_spec(_spec(without_link)), WARNING) == set()
    assert "spatial-link-without-position" in _codes(lint_spec(_spec(no_position)), WARNING)


def test_bearer_fields_under_subject_or_unknown_warn() -> None:
    text = _CLEAN.replace("raw_source_path: field(Source)}",
                          "raw_source_path: field(Source), bearer_entity_type: const(poi)}")
    assert "bearer-without-party" in _codes(lint_spec(_spec(text)), WARNING)


def test_half_coordinate_warns() -> None:
    text = _CLEAN.replace("latitude: field(Lat), longitude: field(Lon)",
                          "latitude: field(Lat)")
    assert "half-coordinate" in _codes(lint_spec(_spec(text)), WARNING)


def test_missing_link_warns() -> None:
    text = _CLEAN.replace(", temporal: at_least_once}", "}")
    assert "missing-link" in _codes(lint_spec(_spec(text)), WARNING)


def test_naive_timezone_advises() -> None:
    text = _CLEAN.replace("epoch: unix_s", 'format: "%Y-%m-%d"')
    assert "naive-timezone" in _codes(lint_spec(_spec(text)), ADVICE)


def test_tier_tool_and_os_version_advice() -> None:
    text = _CLEAN.replace("tool: AXIOM, ", "").replace('os_version: ">=15", ', "")
    codes = _codes(lint_spec(_spec(text)), ADVICE)
    assert "tier-tool" in codes        # secondary but no tool
    assert "no-os-version" in codes


def test_id_naming_advice() -> None:
    text = _CLEAN.replace("id: ios.demo.clean", "id: NotASlug")
    assert "id-naming" in _codes(lint_spec(_spec(text)), ADVICE)


def test_raw_epoch_arithmetic_advice() -> None:
    text = _CLEAN.replace(
        "latitude: field(Lat)",
        'latitude: { from: field(Lat), pipe: "arithmetic((value + 978307200) * 1000000000)" }')
    assert "raw-epoch-arithmetic" in _codes(lint_spec(_spec(text)), ADVICE)


def test_no_expected_fields_advises() -> None:
    text = _CLEAN.replace('expected_fields: [Lat, Lon, TS, Loc, "Item ID", Source]\n', "")
    assert "no-expected-fields" in _codes(lint_spec(_spec(text)), ADVICE)


def test_mapped_not_declared_warns() -> None:
    # Drop "TS" from the inventory while it is still mapped by the time block.
    text = _CLEAN.replace('expected_fields: [Lat, Lon, TS, Loc, "Item ID", Source]',
                          'expected_fields: [Lat, Lon, Loc, "Item ID", Source]')
    findings = lint_spec(_spec(text))
    assert "mapped-not-declared" in _codes(findings, WARNING)


def test_expected_fields_glob_covers_mapped_field() -> None:
    # A glob inventory entry covers the mapped column, so no warning.
    text = _CLEAN.replace('expected_fields: [Lat, Lon, TS, Loc, "Item ID", Source]',
                          'expected_fields: [Lat, Lon, "T*", Loc, "Item ID", Source]')
    assert "mapped-not-declared" not in _codes(lint_spec(_spec(text)), WARNING)


# --- new-schema checks ----------------------------------------------------

def test_clean_preset_is_free_of_new_warnings() -> None:
    codes = _codes(lint_spec(_spec(_CLEAN)), WARNING)
    assert {"unfilled-placeholder", "literal-uuid-in-path", "multi-role-unsupported"} & codes == set()


def test_unfilled_placeholder_is_flagged() -> None:
    text = _CLEAN.replace("longitude: field(Lon)", 'longitude: field("TODO")')
    findings = lint_spec(_spec(text))
    assert "unfilled-placeholder" in _codes(findings, WARNING)


def test_literal_uuid_in_path_is_flagged() -> None:
    text = _CLEAN.replace(
        "input_selector: {format: csv, name: c.csv}",
        "input_selector: {format: sqlite, "
        "path: '/private/var/mobile/Containers/Data/Application/"
        "005DDA28-C17A-4079-BBB6-E6255870D163/Documents/store.sqlite', table: T}",
    )
    assert "literal-uuid-in-path" in _codes(lint_spec(_spec(text)), WARNING)


def test_uuid_token_in_path_is_not_flagged() -> None:
    text = _CLEAN.replace(
        "input_selector: {format: csv, name: c.csv}",
        "input_selector: {format: sqlite, "
        "path: '/private/var/mobile/Containers/Data/Application/{uuid}/Documents/store.sqlite', table: T}",
    )
    assert "literal-uuid-in-path" not in _codes(lint_spec(_spec(text)))


def test_multi_role_preset_is_flagged() -> None:
    text = _CLEAN.replace(
        "input_selector: {format: csv, name: c.csv}",
        "input_selector:\n"
        "  - {role: messages, format: csv, name: a.csv}\n"
        "  - {role: account, format: csv, name: b.csv}\n",
    )
    assert "multi-role-unsupported" in _codes(lint_spec(_spec(text)), WARNING)


# --- the raw-document pass: diagnosing a preset that does NOT load -------------------

def _raw_codes(text: str, tmp_path: Path, severity=None) -> set[str]:
    file = tmp_path / "p.yaml"
    file.write_text(text, encoding="utf-8")
    return _codes(lint_file(file), severity)


def _finding(text: str, tmp_path: Path, code: str):
    file = tmp_path / "p.yaml"
    file.write_text(text, encoding="utf-8")
    matches = [f for f in lint_file(file) if f.code == code]
    assert matches, f"no {code} finding among {[f.code for f in lint_file(file)]}"
    return matches[0]


def test_misplaced_model_field_names_the_common_block(tmp_path: Path) -> None:
    """The failure that motivated the pass: an assignable field written at the top level
    parses as an unknown key, and the parser's error is the only thing reported."""
    finding = _finding(_CLEAN + "input_record_id: field(Row)\n", tmp_path, "misplaced-top-level")
    assert finding.severity == ERROR
    assert "common:" in finding.suggestion and "input_record_id: field(Row)" in finding.suggestion


def test_unknown_top_level_key_suggests_the_closest_admitted_one(tmp_path: Path) -> None:
    finding = _finding(_CLEAN + "expected_field: [A]\n", tmp_path, "unknown-top-level")
    assert "expected_fields" in finding.suggestion


def test_conditional_under_links_is_reported_with_the_rewrite(tmp_path: Path) -> None:
    """``links:`` takes bare enum tokens; a conditional there is a load error whose message
    does not say where the value belongs instead."""
    text = _CLEAN.replace(
        "temporal: at_least_once}",
        "temporal: if field(Dur) == 0 then const(at_least_once) else const(throughout)}")
    finding = _finding(text, tmp_path, "conditional-in-links")
    assert "temporal_link:" in finding.suggestion
    assert "position:" in finding.suggestion


def test_glob_in_selector_name_is_flagged(tmp_path: Path) -> None:
    """``name:`` is EXACT basename equality — a wildcard silently matches nothing, so the
    preset loads cleanly and then never fires."""
    text = _CLEAN.replace("name: c.csv}", 'name: "*AllData.csv"}')
    finding = _finding(text, tmp_path, "glob-in-selector-name")
    assert finding.severity == WARNING
    assert "AllData.csv" in finding.suggestion
    # The ordinary exact name is not flagged.
    assert "glob-in-selector-name" not in _raw_codes(_CLEAN, tmp_path)


# --- silent-degradation checks ------------------------------------------------------

def test_unrecognised_free_text_vocabulary_warns_with_the_nearest_member() -> None:
    """These four columns are free TEXT: nothing rejects a typo, the value is just never
    interpreted — so the linter is the only place it can surface."""
    text = _CLEAN.replace("entity: const(device)", "entity: const(x), entity_type: const(devices)")
    findings = [f for f in lint_spec(_spec(text)) if f.code == "free-text-vocab"]
    assert findings and findings[0].severity == WARNING
    assert findings[0].suggestion == "entity_type: const(device)"
    # A recognised member is silent.
    ok = _CLEAN.replace("entity: const(device)", "entity: const(x), entity_type: const(device)")
    assert "free-text-vocab" not in _codes(lint_spec(_spec(ok)))


def test_fixed_offset_zone_advises_naming_the_iana_zone() -> None:
    text = _CLEAN.replace("epoch: unix_s", 'format: "%Y-%m-%d", zone: const(+01:00)')
    findings = [f for f in lint_spec(_spec(text)) if f.code == "fixed-offset-zone"]
    assert findings and findings[0].severity == ADVICE
    # A named zone is the recommendation, so it must not flag itself.
    named = _CLEAN.replace("epoch: unix_s", 'format: "%Y-%m-%d", zone: const(Europe/Zurich)')
    assert "fixed-offset-zone" not in _codes(lint_spec(_spec(named)))


_DURATION_TIME = ('time: {interval: {lower: field(TS), '
                  'duration_sec: {from: field(Dur), unit: s}}, format: "%Y-%m-%d", '
                  'zone: const(Europe/Zurich)}')


def test_duration_unit_mismatch_advice() -> None:
    """The column name betrays the unit: a `_ms` column declared as seconds would inflate
    every derived end bound a thousandfold."""
    text = _CLEAN.replace("time: {instant: field(TS), epoch: unix_s}",
                          _DURATION_TIME.replace("field(Dur)", "field(Call_ms)"))
    findings = [f for f in lint_spec(_spec(text)) if f.code == "duration-unit-mismatch"]
    assert findings and findings[0].suggestion.startswith("unit: ms")


def test_implicit_record_uid_advice() -> None:
    """Omission and oversight read identically, so the linter asks for the intent to be
    stated — and goes quiet once it is, whichever way."""
    text = _CLEAN.replace("source_record_uid: 'field(\"Item ID\")'\n", "")
    assert "implicit-record-uid" in _codes(lint_spec(_spec(text)), ADVICE)
    stated = text.replace("expected_fields:", "source_record_uid: default\nexpected_fields:")
    assert "implicit-record-uid" not in _codes(lint_spec(_spec(stated)))
    assert "implicit-record-uid" not in _codes(lint_spec(_spec(_CLEAN)))


def test_findings_render_the_suggestion_as_a_fix_line() -> None:
    text = _CLEAN.replace(", spatial_referential: const(epsg:4326)", "")
    rendered = [f.format() for f in lint_spec(_spec(text)) if f.code == "coords-no-referential"][0]
    assert "→ fix: spatial_referential: const(epsg:4326)" in rendered
