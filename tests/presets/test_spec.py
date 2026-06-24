"""Tests for the v3 declarative preset schema (presets.spec) with input_selector.

Used by:    pytest.
Depends on: model_atlas.presets.spec, pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_atlas.presets.spec import preset_spec_from_yaml

_VALID = """
preset:
  id: ios.routined.cached_locations
  name: Cached Locations
  os: iOS
  tool:
  os_version: ">=15"
  version: 1.0
  tier: primary
input_selector:
  format: sqlite
  path: /private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite
  table: ZRTCLLOCATIONMO
patterns:
  coords: "(?P<lat>-?\\\\d+\\\\.\\\\d+) - (?P<lon>-?\\\\d+\\\\.\\\\d+)"
lookup_tables:
  recovery: {Parsing: intact}
source_record_uid: column(Z_PK)
common:
  entity: const(device)
  source_label: const(ZRTCLLOCATIONMO)
assertions:
  - position:
      latitude_wgs84: column(ZLATITUDE)
      horizontal_speed_kmh: { from: column(ZSPEED), unit: m/s }
    time:
      instant: column(ZTIMESTAMP)
      epoch: cocoa
      zone: const(UTC)
    links:
      entity_position: at
      entity_time: observed_at
      spatial_temporal: instant
"""


def _parse(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("test.yaml"))


def test_valid_preset_parses() -> None:
    preset = _parse(_VALID)
    assert preset.meta.id == "ios.routined.cached_locations"
    assert preset.name == "iOS — Cached Locations"          # composed title (empty tool)
    assert preset.source_tier == "primary"
    selector = preset.input_selectors[0]
    assert selector.format == "sqlite" and selector.table == "ZRTCLLOCATIONMO"
    assert selector.path.endswith("Cache.sqlite") and selector.role == "source"
    assert preset.source_record_uid is not None and preset.source_record_uid.ref.arg == "Z_PK"
    template = preset.assertions[0]
    fields = {f.model_field: f for f in template.fields}
    assert fields["latitude_wgs84"].ref.arg == "ZLATITUDE"
    assert fields["horizontal_speed_kmh"].unit == "m/s"
    assert fields["entity_position_link"].ref.arg == "at"
    time = template.temporal[0]
    assert time.kind == "instant" and time.epoch == "cocoa"


def test_title_with_tool() -> None:
    preset = _parse(_VALID.replace("  tool:\n", "  tool: AXIOM\n"))
    assert preset.name == "iOS AXIOM — Cached Locations"


def test_invalid_edge_value_rejected() -> None:
    with pytest.raises(ValueError):
        _parse(_VALID.replace("entity_position: at", "entity_position: teleported"))


def test_engine_owned_field_not_assignable() -> None:
    bad = _VALID.replace("latitude_wgs84: column(ZLATITUDE)", "time_lower_unix_utc_us: column(ZLATITUDE)")
    with pytest.raises(ValueError):
        _parse(bad)


# --- more valid shapes ------------------------------------------------------

_MIN = """
preset: {id: t, name: N, version: 1.0, tier: secondary}
input_selector: {format: csv, name: f.csv}
assertions:
  - position: {latitude_wgs84: column(Lat)}
    time: {instant: column(TS), format: "%Y"}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_minimal_preset_parses() -> None:
    preset = _parse(_MIN)
    assert preset.meta.id == "t" and preset.name == "N"
    assert preset.source_record_uid is None                   # omitted -> generated UID
    selector = preset.input_selectors[0]
    assert selector.format == "csv" and selector.name == "f.csv"


def test_excel_and_sqlite_selector_parse() -> None:
    excel = _parse(_MIN.replace("input_selector: {format: csv, name: f.csv}",
                                "input_selector: {format: excel, name: x.xlsx, sheet: S}"))
    assert excel.input_selectors[0].sheet == "S"
    assert excel.input_selectors[0].reader_params()["sheet"] == "S"
    sqlite = _parse(_MIN.replace("input_selector: {format: csv, name: f.csv}",
                                 "input_selector: {format: sqlite, name: d.sqlite, table: T}"))
    assert sqlite.input_selectors[0].reader_params()["table"] == "T"


def test_selector_path_recorded() -> None:
    sqlite = _parse(_MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: sqlite, path: /a/b/d.sqlite, table: T}"))
    assert sqlite.input_selectors[0].path == "/a/b/d.sqlite"


def test_selector_list_or_alternatives() -> None:
    text = _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector:\n"
        "  - {format: csv, name: a.csv}\n"
        "  - {format: csv, name: b.csv}\n",
    )
    preset = _parse(text)
    assert len(preset.input_selectors) == 2
    assert preset.roles == ("source",)              # role-less entries share one OR group


def test_distinct_roles_recorded() -> None:
    text = _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector:\n"
        "  - {role: messages, format: sqlite, name: sms.db, table: m}\n"
        "  - {role: account, format: csv, name: acct.csv}\n",
    )
    assert _parse(text).roles == ("messages", "account")


def test_expected_columns_parsed_including_globs() -> None:
    text = _MIN.replace(
        "assertions:",
        'expected_columns: [Lat, TS, "Timestamp - * (dd)"]\nassertions:',
    )
    assert _parse(text).expected_columns == ("Lat", "TS", "Timestamp - * (dd)")


# --- invalid shapes that MUST be rejected -----------------------------------

_INVALID: dict[str, str] = {
    "preset header missing": _MIN.replace("preset:", "header:"),
    "id missing": _MIN.replace("id: t, ", ""),
    "name missing": _MIN.replace("name: N, ", ""),
    "bad tier": _MIN.replace("tier: secondary", "tier: bogus"),
    "input_selector missing": _MIN.replace("input_selector:", "input_selectorx:"),
    "bad format": _MIN.replace("format: csv", "format: parquet"),
    "neither path nor name": _MIN.replace("input_selector: {format: csv, name: f.csv}",
                                          "input_selector: {format: csv}"),
    "both path and name": _MIN.replace("input_selector: {format: csv, name: f.csv}",
                                       "input_selector: {format: csv, name: f.csv, path: /a/f.csv}"),
    "double-star path rejected": _MIN.replace("input_selector: {format: csv, name: f.csv}",
                                              "input_selector: {format: csv, path: /a/**/f.csv}"),
    "bad placeholder rejected": _MIN.replace("input_selector: {format: csv, name: f.csv}",
                                             "input_selector: {format: csv, path: '/a/{id}/f.csv'}"),
    "sqlite needs table xor sql": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: sqlite, name: d.sqlite}"),
    "sqlite both table and sql": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: sqlite, name: d.sqlite, table: T, sql: 'SELECT 1'}"),
    "excel needs sheet": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: excel, name: x.xlsx}"),
    "assertions empty": _MIN.split("assertions:")[0] + "assertions: []\n",
    "assertion missing time": _MIN.replace(
        '    time: {instant: column(TS), format: "%Y"}\n', ""),
    "time both instant and interval": _MIN.replace(
        'instant: column(TS), format: "%Y"',
        'instant: column(TS), interval: {lower: column(A), upper: column(B)}'),
    "time epoch and format": _MIN.replace('format: "%Y"', 'format: "%Y", epoch: cocoa'),
    "unknown links key": _MIN.replace("spatial_temporal: instant}",
                                      "spatial_temporal: instant, bogus: x}"),
    "invalid entity_position enum": _MIN.replace("entity_position: at",
                                                 "entity_position: teleported"),
    "engine-owned field assigned": _MIN.replace("latitude_wgs84: column(Lat)",
                                                "latitude_source_field: column(Lat)"),
    "unknown model field": _MIN.replace("latitude_wgs84: column(Lat)",
                                        "not_a_field: column(Lat)"),
    "bare reference": _MIN.replace("latitude_wgs84: column(Lat)", "latitude_wgs84: Lat"),
    "invalid regex pattern": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: csv, name: f.csv}\npatterns: {bad: \"(unclosed\"}"),
    "expected_columns not a list": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: csv, name: f.csv}\nexpected_columns: Lat"),
}


@pytest.mark.parametrize("case", list(_INVALID), ids=list(_INVALID))
def test_invalid_presets_are_rejected(case: str) -> None:
    with pytest.raises(ValueError):
        _parse(_INVALID[case])


def test_shipped_presets_all_load() -> None:
    """Guard: every preset committed under presets/ must parse and validate."""
    from model_atlas.presets.spec_loader import load_preset_specs

    specs = load_preset_specs(Path(__file__).resolve().parents[2] / "presets")
    assert len(specs) >= 5
    ids = [s.meta.id for s in specs]
    assert len(ids) == len(set(ids))  # ids are unique
