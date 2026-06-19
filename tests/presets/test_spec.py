"""Tests for the v3 declarative preset schema (presets.spec).

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
match:
  type: sqlite
  in_archive: /private/var/.../Cache.sqlite
  as_file: Cache.sqlite
  table: ZRTCLLOCATIONMO
patterns:
  coords: "(?P<lat>-?\\\\d+\\\\.\\\\d+) - (?P<lon>-?\\\\d+\\\\.\\\\d+)"
lookup_tables:
  recovery: {Parsing: intact}
record_uid: column(Z_PK)
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
    assert preset.meta.os_version == ">=15"
    assert preset.record_uid is not None and preset.record_uid.ref.arg == "Z_PK"
    common = {f.model_field: f for f in preset.common}
    assert common["entity"].ref.kind == "const" and common["entity"].ref.arg == "device"
    template = preset.assertions[0]
    fields = {f.model_field: f for f in template.fields}
    assert fields["latitude_wgs84"].ref.arg == "ZLATITUDE"
    assert fields["horizontal_speed_kmh"].unit == "m/s"
    # links became three const FieldSpecs on the assertion
    assert fields["entity_position_link"].ref.arg == "at"
    assert fields["spatial_temporal_link"].ref.arg == "instant"
    time = template.temporal[0]
    assert time.kind == "instant" and time.epoch == "cocoa"
    assert time.zone is not None and time.zone.ref.arg == "UTC"


def test_title_with_tool() -> None:
    preset = _parse(_VALID.replace("  tool:\n", "  tool: AXIOM\n"))
    assert preset.name == "iOS AXIOM — Cached Locations"


def test_invalid_edge_value_rejected() -> None:
    with pytest.raises(ValueError):
        _parse(_VALID.replace("entity_position: at", "entity_position: teleported"))


def test_engine_owned_field_not_assignable() -> None:
    bad = _VALID.replace("latitude_wgs84: column(ZLATITUDE)", "time_lower_unix_us: column(ZLATITUDE)")
    with pytest.raises(ValueError):
        _parse(bad)


def test_time_needs_exactly_one_kind() -> None:
    bad = _VALID.replace(
        "instant: column(ZTIMESTAMP)",
        "instant: column(ZTIMESTAMP)\n      interval: {lower: column(A), upper: column(B)}",
    )
    with pytest.raises(ValueError):
        _parse(bad)


def test_epoch_and_format_exclusive() -> None:
    bad = _VALID.replace("epoch: cocoa", 'epoch: cocoa\n      format: "%Y"')
    with pytest.raises(ValueError):
        _parse(bad)


def test_extract_requires_known_pattern_and_named_group() -> None:
    bad = _VALID.replace(
        "latitude_wgs84: column(ZLATITUDE)",
        "latitude_wgs84: { from: column(ZLATITUDE), extract: coords.nope }",
    )
    with pytest.raises(ValueError, match="named group"):
        _parse(bad)


def test_reference_must_be_a_call() -> None:
    with pytest.raises(ValueError):
        _parse(_VALID.replace("entity: const(device)", "entity: device"))


# --- more valid shapes ------------------------------------------------------

_MIN = """
preset: {id: t, name: N, version: 1.0, tier: secondary}
match: {type: csv, as_file: f.csv}
assertions:
  - position: {latitude_wgs84: column(Lat)}
    time: {instant: column(TS), format: "%Y"}
    links: {entity_position: at, entity_time: observed_at, spatial_temporal: instant}
"""


def test_minimal_preset_parses() -> None:
    preset = _parse(_MIN)
    assert preset.meta.id == "t" and preset.name == "N"
    assert preset.record_uid is None                   # omitted -> generated UID
    assert preset.match.source_type == "csv" and preset.match.as_file == "f.csv"


def test_interval_time_parses() -> None:
    text = _MIN.replace('time: {instant: column(TS), format: "%Y"}',
                        'time: {interval: {lower: column(A), upper: column(B)}, epoch: unix_s}')
    spec = _parse(text).assertions[0].temporal[0]
    assert spec.kind == "interval" and spec.epoch == "unix_s"
    assert spec.lower.arg == "A" and spec.upper.arg == "B"


def test_excel_and_sqlite_match_parse() -> None:
    excel = _parse(_MIN.replace("match: {type: csv, as_file: f.csv}",
                                "match: {type: excel, as_file: x.xlsx, sheet: S}"))
    assert excel.match.sheet == "S" and excel.extract["excel"]["sheet_name"] == "S"
    sqlite = _parse(_MIN.replace("match: {type: csv, as_file: f.csv}",
                                 "match: {type: sqlite, as_file: d.sqlite, table: T}"))
    assert sqlite.extract["sqlite"]["table"] == "T"


def test_expected_columns_parsed_including_globs() -> None:
    text = _MIN.replace(
        "assertions:",
        'expected_columns: [Lat, TS, "Timestamp - * (dd)"]\nassertions:',
    )
    assert _parse(text).expected_columns == ("Lat", "TS", "Timestamp - * (dd)")


def test_lookup_tables_and_patterns_stored() -> None:
    text = _MIN.replace(
        "assertions:",
        "lookup_tables: {rec: {Parsing: intact}}\npatterns: {p: \"(?P<g>x)\"}\nassertions:",
    )
    preset = _parse(text)
    assert preset.lookup_tables == {"rec": {"Parsing": "intact"}}
    assert preset.patterns == {"p": "(?P<g>x)"}


# --- invalid shapes that MUST be rejected -----------------------------------

_INVALID: dict[str, str] = {
    "preset header missing": _MIN.replace("preset:", "header:"),
    "id missing": _MIN.replace("id: t, ", ""),
    "name missing": _MIN.replace("name: N, ", ""),
    "bad tier": _MIN.replace("tier: secondary", "tier: bogus"),
    "match missing": _MIN.replace("match:", "matchx:"),
    "bad source type": _MIN.replace("type: csv", "type: parquet"),
    "assertions empty": _MIN.split("assertions:")[0] + "assertions: []\n",
    "assertion missing time": _MIN.replace(
        '    time: {instant: column(TS), format: "%Y"}\n', ""),
    "time both instant and interval": _MIN.replace(
        'instant: column(TS), format: "%Y"',
        'instant: column(TS), interval: {lower: column(A), upper: column(B)}'),
    "time neither instant nor interval": _MIN.replace(
        'instant: column(TS), format: "%Y"', 'zone: const(UTC)'),
    "time epoch and format": _MIN.replace('format: "%Y"', 'format: "%Y", epoch: cocoa'),
    "unknown epoch": _MIN.replace('instant: column(TS), format: "%Y"',
                                  'instant: column(TS), epoch: bogus'),
    "interval missing upper": _MIN.replace(
        'instant: column(TS), format: "%Y"',
        'interval: {lower: column(A)}, format: "%Y"'),
    "unknown links key": _MIN.replace("spatial_temporal: instant}",
                                      "spatial_temporal: instant, bogus: x}"),
    "invalid entity_position enum": _MIN.replace("entity_position: at",
                                                 "entity_position: teleported"),
    "invalid entity_time enum": _MIN.replace("entity_time: observed_at",
                                             "entity_time: napping"),
    "invalid spatial_temporal enum": _MIN.replace("spatial_temporal: instant}",
                                                  "spatial_temporal: forever}"),
    "engine-owned field assigned": _MIN.replace("latitude_wgs84: column(Lat)",
                                                "latitude_source_field: column(Lat)"),
    "unknown model field": _MIN.replace("latitude_wgs84: column(Lat)",
                                        "not_a_field: column(Lat)"),
    "bare reference": _MIN.replace("latitude_wgs84: column(Lat)", "latitude_wgs84: Lat"),
    "mapping missing from": _MIN.replace("latitude_wgs84: column(Lat)",
                                         "latitude_wgs84: {unit: m/s}"),
    "extract unknown pattern": _MIN.replace(
        "latitude_wgs84: column(Lat)",
        "latitude_wgs84: {from: column(C), extract: nope.x}"),
    "extract missing dot": _MIN.replace(
        "latitude_wgs84: column(Lat)",
        "latitude_wgs84: {from: column(C), extract: nodot}"),
    "patterns not a mapping": _MIN.replace(
        "match: {type: csv, as_file: f.csv}",
        "match: {type: csv, as_file: f.csv}\npatterns: [a, b]"),
    "lookup_tables not table-of-tables": _MIN.replace(
        "match: {type: csv, as_file: f.csv}",
        "match: {type: csv, as_file: f.csv}\nlookup_tables: {x: 5}"),
    "invalid regex pattern": _MIN.replace(
        "match: {type: csv, as_file: f.csv}",
        "match: {type: csv, as_file: f.csv}\npatterns: {bad: \"(unclosed\"}"),
    "expected_columns not a list": _MIN.replace(
        "match: {type: csv, as_file: f.csv}",
        "match: {type: csv, as_file: f.csv}\nexpected_columns: Lat"),
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
