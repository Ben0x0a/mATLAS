"""Declarative preset schema for the pluggable pipeline (v2).

Defines:    the typed preset structures (FieldSpec, TemporalSpec, AssertionTemplate,
            PresetSpec) and preset_spec_from_yaml() which parses + validates a YAML
            preset against the canonical model column names and edge vocabularies.
Used by:    the v2 loader, the assembly engine, and validation.
Depends on: model.families (OUTPUT_COLUMNS + edge enums for validation).

Preset layout::

    name: ...
    parser: {name, version}
    source_tier: secondary            # optional, validated against SourceTier
    selectors: [{source_type: csv, file_name: "Cached Locations.csv"}]
    extract: {csv: {delimiter: ","}}
    expected_columns: [Latitude, Longitude, ...]
    source_row_id: {from: "Item ID"}  # optional; engine generates one if absent
    common:                           # fields shared by every assertion from a row
      entity: {value: device}
      tool_label: {value: ZRTCLLOCATIONMO}
    assertions:                       # each entry x each temporal spec -> one assertion
      - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
        longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
        entity_position_link: at
        temporal:
          - instant: "Timestamp ..."
            pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
            entity_time_link: observed_at
            spatial_temporal_link: instant
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from model_atlas.model.families import (
    OUTPUT_COLUMNS,
    EntityPositionLink,
    EntityTimeLink,
    RecordState,
    SourceTier,
    SpatialTemporalLink,
)

# Columns produced by the engine, not assignable in common/assertions:
# the temporal bounds come from temporal specs, source_row_id from its own spec,
# and the derived columns from untangle.
_ENGINE_OWNED_FIELDS: frozenset[str] = frozenset({
    "time_lower_raw",
    "time_lower_source_field",
    "time_lower_unix_ns",
    "time_upper_raw",
    "time_upper_source_field",
    "time_upper_unix_ns",
    "source_row_id",
    "record_type",
    "record_rank",
})
ASSIGNABLE_FIELDS: frozenset[str] = frozenset(OUTPUT_COLUMNS) - _ENGINE_OWNED_FIELDS

# Fields whose constant value must belong to a controlled vocabulary.
_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "entity_position_link": frozenset(e.value for e in EntityPositionLink),
    "entity_time_link": frozenset(e.value for e in EntityTimeLink),
    "spatial_temporal_link": frozenset(e.value for e in SpatialTemporalLink),
    "source_tier": frozenset(e.value for e in SourceTier),
    "deleted": frozenset(e.value for e in RecordState),
}

_TEMPORAL_STRUCTURAL_KEYS = frozenset({"instant", "interval", "pipe"})


@dataclass(frozen=True)
class ParserInfo:
    name: str
    version: str


@dataclass(frozen=True)
class FieldSpec:
    """How one model field is populated.

    Exactly one source: ``column`` (read a source column's value; the name may be a
    glob, e.g. ``"Timestamp Date/Time - * (dd.MM.yyyy)"``), ``from_name_pattern``
    (yield the matched column's *name* as the value — pipe it to extract, say, the
    timezone embedded in the header), or a constant ``value``.
    """

    model_field: str
    column: str | None = None
    from_name_pattern: str | None = None
    value: Any = None
    is_constant: bool = False
    pipe: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TemporalSpec:
    """One temporal facet of an assertion: an instant (lower == upper) or an interval.

    ``pipe`` converts the timestamp column(s); ``overrides`` carry per-spec field
    values such as the temporal bindings.
    """

    kind: str  # "instant" | "interval"
    lower_column: str
    upper_column: str
    pipe: tuple[dict[str, Any], ...] = ()
    overrides: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class AssertionTemplate:
    """Shared (spatial/entity/binding) fields plus a list of temporal specs.

    One source row produces one assertion per temporal spec, sharing ``fields``.
    """

    fields: tuple[FieldSpec, ...]
    temporal: tuple[TemporalSpec, ...]


@dataclass(frozen=True)
class PresetSpec:
    name: str
    version: str
    parser: ParserInfo
    path: Path
    source_tier: str | None = None
    selectors: tuple[dict[str, Any], ...] = ()
    extract: dict[str, Any] = field(default_factory=dict)
    expected_columns: tuple[str, ...] = ()
    source_row_id: FieldSpec | None = None
    common: tuple[FieldSpec, ...] = ()
    assertions: tuple[AssertionTemplate, ...] = ()


def _require_str(raw: dict[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: missing non-empty string key {key!r}")
    return value


def _parse_pipe(raw_value: Any, path: Path, where: str) -> tuple[dict[str, Any], ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list) or not all(isinstance(step, dict) for step in raw_value):
        raise ValueError(f"{path}: {where} pipe must be a list of mappings")
    return tuple(raw_value)


def _validate_enum(model_field: str, value: Any, path: Path) -> None:
    allowed = _ENUM_FIELDS.get(model_field)
    if allowed is not None and value is not None and value not in allowed:
        raise ValueError(
            f"{path}: {model_field!r} value {value!r} is not one of {sorted(allowed)}"
        )


def _parse_field(model_field: str, raw_value: Any, path: Path, *, assignable: bool = True) -> FieldSpec:
    if assignable and model_field not in ASSIGNABLE_FIELDS:
        raise ValueError(f"{path}: {model_field!r} is not an assignable model field")
    if isinstance(raw_value, dict):
        has_from = "from" in raw_value
        has_name = "from_name" in raw_value
        has_value = "value" in raw_value
        if has_from + has_name + has_value != 1:
            raise ValueError(
                f"{path}: {model_field!r} mapping must set exactly one of 'from', 'from_name', 'value'"
            )
        pipe = _parse_pipe(raw_value.get("pipe"), path, model_field)
        if has_from:
            column = raw_value["from"]
            if not isinstance(column, str):
                raise ValueError(f"{path}: {model_field!r} 'from' must be a column name (glob allowed)")
            return FieldSpec(model_field=model_field, column=column, pipe=pipe)
        if has_name:
            pattern = raw_value["from_name"]
            if not isinstance(pattern, str):
                raise ValueError(f"{path}: {model_field!r} 'from_name' must be a column-name pattern")
            return FieldSpec(model_field=model_field, from_name_pattern=pattern, pipe=pipe)
        value = raw_value["value"]
        _validate_enum(model_field, value, path)
        return FieldSpec(model_field=model_field, value=value, is_constant=True, pipe=pipe)
    # A bare scalar is a constant (e.g. an edge: `entity_position_link: at`).
    _validate_enum(model_field, raw_value, path)
    return FieldSpec(model_field=model_field, value=raw_value, is_constant=True)


def _parse_temporal_spec(raw: Any, path: Path) -> TemporalSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each temporal spec must be a mapping")
    has_instant = "instant" in raw
    has_interval = "interval" in raw
    if has_instant == has_interval:
        raise ValueError(f"{path}: a temporal spec needs exactly one of 'instant' or 'interval'")
    pipe = _parse_pipe(raw.get("pipe"), path, "temporal")
    overrides = tuple(
        _parse_field(key, value, path)
        for key, value in raw.items()
        if key not in _TEMPORAL_STRUCTURAL_KEYS
    )
    if has_instant:
        column = raw["instant"]
        if not isinstance(column, str):
            raise ValueError(f"{path}: 'instant' must be a column name string")
        return TemporalSpec(kind="instant", lower_column=column, upper_column=column, pipe=pipe, overrides=overrides)
    interval = raw["interval"]
    if not isinstance(interval, dict) or "lower" not in interval or "upper" not in interval:
        raise ValueError(f"{path}: 'interval' must be a mapping with 'lower' and 'upper'")
    lower, upper = interval["lower"], interval["upper"]
    if not isinstance(lower, str) or not isinstance(upper, str):
        raise ValueError(f"{path}: interval 'lower'/'upper' must be column name strings")
    return TemporalSpec(kind="interval", lower_column=lower, upper_column=upper, pipe=pipe, overrides=overrides)


def _parse_assertion(raw: Any, path: Path) -> AssertionTemplate:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each assertions entry must be a mapping")
    temporal_raw = raw.get("temporal")
    if temporal_raw is None:
        raise ValueError(f"{path}: each assertions entry must declare 'temporal'")
    if isinstance(temporal_raw, dict):
        temporal_raw = [temporal_raw]
    if not isinstance(temporal_raw, list) or not temporal_raw:
        raise ValueError(f"{path}: 'temporal' must be a non-empty list or a single mapping")
    fields = tuple(
        _parse_field(key, value, path) for key, value in raw.items() if key != "temporal"
    )
    temporal = tuple(_parse_temporal_spec(spec, path) for spec in temporal_raw)
    return AssertionTemplate(fields=fields, temporal=temporal)


def preset_spec_from_yaml(raw_obj: object, path: Path) -> PresetSpec:
    if not isinstance(raw_obj, dict):
        raise ValueError(f"{path}: preset must be a YAML mapping")
    raw = raw_obj

    parser_raw = raw.get("parser")
    if not isinstance(parser_raw, dict):
        raise ValueError(f"{path}: missing parser mapping")
    parser = ParserInfo(
        name=_require_str(parser_raw, "name", path),
        version=_require_str(parser_raw, "version", path),
    )

    selectors = raw.get("selectors") or ()
    if not isinstance(selectors, list) or not selectors:
        raise ValueError(f"{path}: selectors must be a non-empty list")

    source_tier = raw.get("source_tier")
    if source_tier is not None:
        _validate_enum("source_tier", source_tier, path)

    extract = raw.get("extract") or {}
    if not isinstance(extract, dict):
        raise ValueError(f"{path}: extract must be a mapping")

    expected = raw.get("expected_columns")
    if expected is None:
        expected = []
    if not isinstance(expected, list):
        raise ValueError(f"{path}: expected_columns must be a list")

    source_row_id_raw = raw.get("source_row_id")
    source_row_id = (
        _parse_field("source_row_id", source_row_id_raw, path, assignable=False)
        if source_row_id_raw is not None
        else None
    )

    common_raw = raw.get("common") or {}
    if not isinstance(common_raw, dict):
        raise ValueError(f"{path}: common must be a mapping")
    common = tuple(_parse_field(key, value, path) for key, value in common_raw.items())

    assertions_raw = raw.get("assertions")
    if not isinstance(assertions_raw, list) or not assertions_raw:
        raise ValueError(f"{path}: assertions must be a non-empty list")
    assertions = tuple(_parse_assertion(entry, path) for entry in assertions_raw)

    return PresetSpec(
        name=_require_str(raw, "name", path),
        version=str(raw.get("version") or parser.version),
        parser=parser,
        path=path,
        source_tier=source_tier,
        selectors=tuple(dict(s) for s in selectors),
        extract=dict(extract),
        expected_columns=tuple(str(c) for c in expected),
        source_row_id=source_row_id,
        common=common,
        assertions=assertions,
    )
