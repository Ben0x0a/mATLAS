"""Tests for the preset linter (presets.lint).

Covers the ERROR path (unparseable presets) and each WARNING/ADVICE check, plus a
clean preset producing no errors/warnings and the shipped presets being error-free.
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
expected_columns: [Lat, Lon, TS, Loc, "Item ID", Source]
source_record_uid: 'column("Item ID")'
common: {entity: const(device), input_record_id: column(Loc), raw_source_path: column(Source)}
assertions:
  - position: {latitude_wgs84: column(Lat), longitude_wgs84: column(Lon)}
    time: {instant: column(TS), epoch: unix_s}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
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
    text = _CLEAN.replace("source_record_uid: 'column(\"Item ID\")'", "source_record_uid: column(Z_PK)")
    assert "rowid-as-uid" in _codes(lint_spec(_spec(text)), WARNING)


def test_half_coordinate_warns() -> None:
    text = _CLEAN.replace("latitude_wgs84: column(Lat), longitude_wgs84: column(Lon)",
                          "latitude_wgs84: column(Lat)")
    assert "half-coordinate" in _codes(lint_spec(_spec(text)), WARNING)


def test_missing_link_warns() -> None:
    text = _CLEAN.replace(", spatial_temporal: instant}", "}")
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
        "latitude_wgs84: column(Lat)",
        'latitude_wgs84: { from: column(Lat), pipe: "arithmetic((value + 978307200) * 1000000000)" }')
    assert "raw-epoch-arithmetic" in _codes(lint_spec(_spec(text)), ADVICE)


def test_no_expected_columns_advises() -> None:
    text = _CLEAN.replace('expected_columns: [Lat, Lon, TS, Loc, "Item ID", Source]\n', "")
    assert "no-expected-columns" in _codes(lint_spec(_spec(text)), ADVICE)


def test_mapped_not_declared_warns() -> None:
    # Drop "TS" from the inventory while it is still mapped by the time block.
    text = _CLEAN.replace('expected_columns: [Lat, Lon, TS, Loc, "Item ID", Source]',
                          'expected_columns: [Lat, Lon, Loc, "Item ID", Source]')
    findings = lint_spec(_spec(text))
    assert "mapped-not-declared" in _codes(findings, WARNING)


def test_expected_columns_glob_covers_mapped_column() -> None:
    # A glob inventory entry covers the mapped column, so no warning.
    text = _CLEAN.replace('expected_columns: [Lat, Lon, TS, Loc, "Item ID", Source]',
                          'expected_columns: [Lat, Lon, "T*", Loc, "Item ID", Source]')
    assert "mapped-not-declared" not in _codes(lint_spec(_spec(text)), WARNING)


def test_lint_paths_over_shipped_presets_has_no_errors() -> None:
    presets_dir = Path(__file__).resolve().parents[2] / "presets"
    findings = lint_paths([presets_dir])
    errors = [f for f in findings if f.severity == ERROR]
    assert errors == [], f"shipped presets have lint errors: {[f.format() for f in errors]}"


# --- new-schema checks ----------------------------------------------------

def test_clean_preset_is_free_of_new_warnings() -> None:
    codes = _codes(lint_spec(_spec(_CLEAN)), WARNING)
    assert {"unfilled-placeholder", "literal-uuid-in-path", "multi-role-unsupported"} & codes == set()


def test_unfilled_placeholder_is_flagged() -> None:
    text = _CLEAN.replace("longitude_wgs84: column(Lon)", 'longitude_wgs84: column("TODO")')
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
