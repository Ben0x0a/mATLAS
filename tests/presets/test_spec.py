"""Tests for the declarative preset schema (presets.spec).

Defines:    valid-parse and validation-failure tests for the v2 preset format.
Used by:    pytest.
Depends on: model_atlas.presets.spec, pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_atlas.presets.spec import preset_spec_from_yaml

_VALID = """
name: iOS Routined Cache Locations
parser: {name: ios_routined_cache_locations, version: "1.0"}
source_tier: secondary
selectors:
  - {source_type: csv, file_name: "Cached Locations.csv"}
extract: {csv: {delimiter: ","}}
expected_columns: [Latitude, Longitude, "Accuracy (m)", Direction]
source_row_id: {from: "Item ID"}
common:
  entity: {value: device}
  tool_label: {value: ZRTCLLOCATIONMO}
assertions:
  - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
    longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
    horizontal_speed_kmh: {from: "Speed (m/s)", pipe: [{cast: float}, {arithmetic: "value * 3.6"}]}
    entity_position_link: at
    temporal:
      - instant: "Timestamp"
        pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
"""


def _parse(text: str):
    return preset_spec_from_yaml(yaml.safe_load(text), Path("test.yaml"))


def test_valid_preset_parses() -> None:
    preset = _parse(_VALID)
    assert preset.name == "iOS Routined Cache Locations"
    assert preset.source_tier == "secondary"
    assert preset.source_row_id is not None and preset.source_row_id.column == "Item ID"
    common = {f.model_field: f for f in preset.common}
    assert common["entity"].value == "device" and common["entity"].is_constant
    assert len(preset.assertions) == 1
    template = preset.assertions[0]
    fields = {f.model_field: f for f in template.fields}
    assert fields["latitude_wgs84"].column == "Latitude"
    assert fields["entity_position_link"].value == "at"
    spec = template.temporal[0]
    assert spec.kind == "instant"
    assert spec.lower_column == spec.upper_column == "Timestamp"
    assert spec.pipe == ({"parse_datetime": "%d.%m.%Y %H:%M:%S.%f"},)
    overrides = {f.model_field: f.value for f in spec.overrides}
    assert overrides["entity_time_link"] == "observed_at"
    assert overrides["spatial_temporal_link"] == "instant"


def test_invalid_edge_value_rejected() -> None:
    bad = _VALID.replace("entity_position_link: at", "entity_position_link: teleported")
    with pytest.raises(ValueError):
        _parse(bad)


def test_engine_owned_field_not_assignable() -> None:
    bad = _VALID.replace(
        "latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}",
        "time_lower_unix_ns: {from: Latitude}",
    )
    with pytest.raises(ValueError):
        _parse(bad)


def test_from_and_value_are_exclusive() -> None:
    bad = _VALID.replace("entity: {value: device}", "entity: {value: device, from: Foo}")
    with pytest.raises(ValueError):
        _parse(bad)


def test_temporal_needs_exactly_one_kind() -> None:
    bad = _VALID.replace(
        'instant: "Timestamp"',
        'instant: "Timestamp"\n        interval: {lower: A, upper: B}',
    )
    with pytest.raises(ValueError):
        _parse(bad)
