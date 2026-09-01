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
source_record_uid: field(Z_PK)
common:
  entity: const(device)
  source_label: const(ZRTCLLOCATIONMO)
observations:
  - position:
      latitude: field(ZLATITUDE)
      horizontal_speed_kmh: { from: field(ZSPEED), unit: m/s }
    time:
      instant: field(ZTIMESTAMP)
      epoch: cocoa
      zone: const(UTC)
    links:
      spatial: at
      entity: self
      temporal: once
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
    template = preset.observations[0]
    fields = {f.model_field: f for f in template.fields}
    assert fields["latitude"].ref.arg == "ZLATITUDE"
    assert fields["horizontal_speed_kmh"].unit == "m/s"
    assert fields["spatial_link"].ref.arg == "at"
    time = template.temporal[0]
    assert time.kind == "instant" and time.epoch == "cocoa"


def test_title_with_tool() -> None:
    preset = _parse(_VALID.replace("  tool:\n", "  tool: AXIOM\n"))
    assert preset.name == "iOS AXIOM — Cached Locations"


def test_invalid_edge_value_rejected() -> None:
    with pytest.raises(ValueError):
        _parse(_VALID.replace("spatial: at", "spatial: teleported"))


def test_engine_owned_field_not_assignable() -> None:
    bad = _VALID.replace("latitude: field(ZLATITUDE)", "time_lower_unix_utc_us: field(ZLATITUDE)")
    with pytest.raises(ValueError):
        _parse(bad)


# --- more valid shapes ------------------------------------------------------

_MIN = """
preset: {id: t, name: N, version: 1.0, tier: secondary}
input_selector: {format: csv, name: f.csv}
observations:
  - position: {latitude: field(Lat)}
    time: {instant: field(TS), format: "%Y"}
    links: {entity: self, spatial: at, temporal: once}
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


def test_expected_fields_parsed_including_globs() -> None:
    text = _MIN.replace(
        "observations:",
        'expected_fields: [Lat, TS, "Timestamp - * (dd)"]\nobservations:',
    )
    assert _parse(text).expected_fields == ("Lat", "TS", "Timestamp - * (dd)")


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
    "observations empty": _MIN.split("observations:")[0] + "observations: []\n",
    "observation missing time": _MIN.replace(
        '    time: {instant: field(TS), format: "%Y"}\n', ""),
    "time both instant and interval": _MIN.replace(
        'instant: field(TS), format: "%Y"',
        'instant: field(TS), interval: {lower: field(A), upper: field(B)}'),
    "time epoch and format": _MIN.replace('format: "%Y"', 'format: "%Y", epoch: cocoa'),
    "unknown links key": _MIN.replace("temporal: once}",
                                      "temporal: once, bogus: x}"),
    "invalid spatial enum": _MIN.replace("spatial: at",
                                                 "spatial: teleported"),
    "engine-owned field assigned": _MIN.replace("latitude: field(Lat)",
                                                "latitude_source_field: field(Lat)"),
    "unknown model field": _MIN.replace("latitude: field(Lat)",
                                        "not_a_field: field(Lat)"),
    "bare reference": _MIN.replace("latitude: field(Lat)", "latitude: Lat"),
    "invalid regex pattern": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: csv, name: f.csv}\npatterns: {bad: \"(unclosed\"}"),
    "expected_fields not a list": _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: csv, name: f.csv}\nexpected_fields: Lat"),
    # extract validation (_parse_extract): must be 'pattern.group', name a known pattern, and
    # reference a named group that pattern actually defines.
    "extract not pattern.group": _MIN.replace(
        "latitude: field(Lat)",
        "latitude: {from: field(Lat), extract: nogroup}"),
    "extract unknown pattern": _MIN.replace(
        "latitude: field(Lat)",
        "latitude: {from: field(Lat), extract: absent.g}"),
    "extract references missing group": _MIN.replace(
        "observations:", "patterns:\n  p: \"(?P<lat>[0-9]+)\"\nobservations:").replace(
        "latitude: field(Lat)",
        "latitude: {from: field(Lat), extract: p.nope}"),
    # field-mapping dict validation (_parse_field): the dict form must carry a 'from' reference.
    "field mapping without from": _MIN.replace(
        "latitude: field(Lat)", "latitude: {type: float}"),
    # The top-level key set is CLOSED: an assignable field written there instead of under
    # `common:` would have its whole mapping silently dropped, so it must fail at load.
    "assignable field at top level": _MIN + "input_record_id: field(Row)\n",
    "context key at top level": _MIN + "context.antenna: field(Cell)\n",
    "unknown top-level key": _MIN + "typo_key: 1\n",
}


@pytest.mark.parametrize("case", list(_INVALID), ids=list(_INVALID))
def test_invalid_presets_are_rejected(case: str) -> None:
    with pytest.raises(ValueError):
        _parse(_INVALID[case])


def test_extended_l1_vocabulary_is_assignable() -> None:
    """A preset may now map the extended L1 targets (entity_role, activity_kind,
    multiplicity, referentials) and any ``context.<key>`` in its ``common:`` block, and a
    spatial referential in a position — not only the programmatic assemble path."""
    text = _MIN.replace(
        "observations:",
        "common:\n"
        "  activity_role: const(caller)\n"
        "  activity_kind: const(call)\n"
        "  identifier_type: const(bssid)\n"
        "  multiplicity: field(N)\n"
        "  context.callee: field(Callee)\n"
        "  context.imei: field(IMEI)\n"
        "observations:",
    ).replace(
        "position: {latitude: field(Lat)}",
        "position: {latitude: field(Lat), spatial_referential: const(epsg:4326)}",
    )
    preset = _parse(text)
    common = {f.model_field: f for f in preset.common}
    assert common["activity_role"].ref.arg == "caller"
    assert common["activity_kind"].ref.arg == "call"
    assert common["identifier_type"].ref.arg == "bssid"   # free identifier type accepted
    assert common["context.callee"].ref.arg == "Callee"
    assert common["context.imei"].ref.arg == "IMEI"
    position = {f.model_field: f for f in preset.observations[0].fields}
    assert position["spatial_referential"].ref.arg == "epsg:4326"


def test_time_bound_extract_parses() -> None:
    """A time bound may carry ``{from: <ref>, extract: pattern.group}`` to pull a time out
    of a messy source cell."""
    text = _MIN.replace(
        "input_selector: {format: csv, name: f.csv}",
        "input_selector: {format: csv, name: f.csv}\n"
        "patterns: {clock: \"(?P<t>[0-9][0-9]:[0-9][0-9])\"}",
    ).replace(
        'time: {instant: field(TS), format: "%Y"}',
        'time: {instant: {from: field(TS), extract: clock.t}, format: "%H:%M"}',
    )
    ts = _parse(text).observations[0].temporal[0]
    assert ts.lower_extract == ("clock", "t") and ts.upper_extract == ("clock", "t")
    # A bound mapping with no 'from' is rejected.
    bad = _MIN.replace('time: {instant: field(TS), format: "%Y"}',
                       'time: {instant: {extract: clock.t}, format: "%H:%M"}')
    with pytest.raises(ValueError):
        _parse(bad)


def test_metric_field_requires_explicit_unit() -> None:
    """A metric field (a CANONICAL_UNIT key, e.g. altitude_m) mapped with NO unit: is rejected:
    a bare mapping's unit is assumed canonical, so a forgotten source unit silently mis-scales
    the value. Declaring unit: (here the canonical `m`, a valid 'already canonical' confirmation)
    parses."""
    no_unit = _MIN.replace(
        "position: {latitude: field(Lat)}",
        "position: {latitude: field(Lat), altitude_m: field(Alt)}")
    with pytest.raises(ValueError):
        _parse(no_unit)
    with_unit = _MIN.replace(
        "position: {latitude: field(Lat)}",
        "position: {latitude: field(Lat), altitude_m: {from: field(Alt), unit: m}}")
    altitude = {f.model_field: f for f in _parse(with_unit).observations[0].fields}["altitude_m"]
    assert altitude.unit == "m" and altitude.ref.arg == "Alt"


def test_source_record_uid_default_token() -> None:
    """``source_record_uid: default`` STATES the engine-generated UID: it parses (a bareword
    is not a reference call, so it must be matched before the ref parser), leaves
    ``source_record_uid`` None exactly like omitting the key, and records the declaration so
    the linter can tell a stated choice from a forgotten mapping."""
    declared = _parse(_MIN + "source_record_uid: default\n")
    assert declared.source_record_uid is None
    assert declared.source_record_uid_default is True
    # Omission is the same UID, a different intent.
    assert _parse(_MIN).source_record_uid_default is False
    # Case/whitespace tolerant, but nothing else is a bareword: a real mapping still parses
    # as a reference and a non-call bareword is still rejected.
    assert _parse(_MIN + "source_record_uid: '  Default '\n").source_record_uid_default is True
    assert _parse(_MIN + "source_record_uid: field(Item)\n").source_record_uid.ref.arg == "Item"
    with pytest.raises(ValueError):
        _parse(_MIN + "source_record_uid: defaults\n")


_INTERVAL = _MIN.replace(
    'time: {instant: field(TS), format: "%Y"}',
    'time: {interval: {lower: field(TS), duration_sec: {from: field(Dur), unit: s}}, '
    'format: "%Y"}')


def test_duration_bound_parses_as_the_upper_with_its_source_unit() -> None:
    """``duration_sec:`` states the upper bound as an elapsed span. It lands in the same
    ``upper`` slot as an end clock — so every existing consumer (field inventory, matching,
    assembly) sees it for free — and only ``duration_unit`` records that it was a span."""
    time = _parse(_INTERVAL).observations[0].temporal[0]
    assert time.kind == "interval"
    assert time.lower.arg == "TS" and time.upper.arg == "Dur"
    assert time.duration_unit == "s"
    # An ordinary end clock leaves the marker unset — the two shapes stay distinguishable.
    plain = _parse(_INTERVAL.replace("duration_sec: {from: field(Dur), unit: s}",
                                     "upper: field(End)")).observations[0].temporal[0]
    assert plain.upper.arg == "End" and plain.duration_unit is None


@pytest.mark.parametrize("unit", ["s", "ms", "us", "min", "h", "d"])
def test_duration_units_supported(unit: str) -> None:
    spec = _parse(_INTERVAL.replace("unit: s}", f"unit: {unit}}}"))
    assert spec.observations[0].temporal[0].duration_unit == unit


def test_duration_bound_rejects_a_missing_or_unknown_unit() -> None:
    """``duration_sec`` is a metric key (canonical: seconds), so it inherits the metric rule:
    the source unit must be declared, and must be one the conversion table knows. Both are
    caught at load — a wrong span would otherwise mis-scale every derived end bound."""
    with pytest.raises(ValueError):
        _parse(_INTERVAL.replace("{from: field(Dur), unit: s}", "field(Dur)"))
    with pytest.raises(ValueError):
        _parse(_INTERVAL.replace("unit: s}", "unit: fortnights}"))


def test_interval_needs_exactly_one_upper_spelling() -> None:
    with pytest.raises(ValueError):   # both
        _parse(_INTERVAL.replace("lower: field(TS),", "lower: field(TS), upper: field(End),"))
    with pytest.raises(ValueError):   # neither
        _parse(_INTERVAL.replace(", duration_sec: {from: field(Dur), unit: s}", ""))


def test_zone_const_must_be_an_offset_or_a_known_zone() -> None:
    """A named IANA zone is admitted; a typo is rejected at LOAD, because an unrecognised zone
    does not fail at conversion — it silently falls back to assuming UTC."""
    named = _MIN.replace('format: "%Y"', 'format: "%Y", zone: const(Europe/Zurich)')
    assert _parse(named).observations[0].temporal[0].zone.ref.arg == "Europe/Zurich"
    for good in ("const(UTC)", "const(+01:00)", "const(local)", 'header("TZ *")'):
        _parse(_MIN.replace('format: "%Y"', f'format: "%Y", zone: {good}'))
    with pytest.raises(ValueError):
        _parse(_MIN.replace('format: "%Y"', 'format: "%Y", zone: const(Europe/Zurish)'))


def test_unknown_field_still_rejected() -> None:
    """The gate stays closed for genuinely-unknown targets and stray ``context`` (no dot)."""
    with pytest.raises(ValueError):
        _parse(_MIN.replace("latitude: field(Lat)", "not_a_field: field(Lat)"))
    with pytest.raises(ValueError):
        _parse(_MIN.replace("latitude: field(Lat)", "context: field(Lat)"))
