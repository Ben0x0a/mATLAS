"""Declarative preset schema (v3) for the pluggable pipeline.

Defines:    the typed preset structures (PresetMeta, InputSelector, FieldSpec, TimeSpec,
            Links, AssertionTemplate, PresetSpec) and preset_spec_from_yaml(), which
            parses + validates a v3 YAML preset against the canonical model.
Used by:    the loader, the assembly engine, the matcher, and reporting.
Depends on: model.families (columns/types/enums), presets.expr (ref + pipe parser),
            sources.pathmatch (selector path validation).

v3 layout (readable, examiner-first)::

    preset:
      id: ios.routined.cached_locations   # stable machine key
      name: Routined Cached Locations      # human name
      os: iOS                              # composes the title
      tool:                                # empty => primary source
      os_version: ">=15"                   # applicability range (tie-break)
      version: 1.0
      tier: primary
    input_selector:                        # one mapping OR a list (same role = OR)
      format: sqlite                       # REQUIRED, magic-verified: csv|excel|sqlite
      path: /private/.../Cache.sqlite      # XOR name; anchored, prefix-tolerant, {uuid}/*
      table: ZRTCLLOCATIONMO
    source_record_uid: column(ArtifactID)  # optional; omit to auto-generate a deterministic per-record UID
    raw_source_path: preset(path)          # where the trace came from (mapped in `common:`)
    lookup_tables: {recovery: {Parsing: intact}}
    patterns: {coords: "(?P<lat>...) - (?P<lon>...)"}
    common:
      entity: const(device)
    assertions:
      - position: {latitude_wgs84: column(ZLATITUDE), ...}
        time:     {instant: column(ZTIMESTAMP), epoch: cocoa, zone: UTC}
        links:    {entity_position: at, entity_time: observed_at, spatial_temporal: instant}

A value is ONE explicit reference call (column/header/filename/param/const) or a
mapping ``{from: <ref>, type:, unit:, extract:, pipe:}``. See presets.expr.
"""
from __future__ import annotations

import re
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
from model_atlas.presets.expr import PipeCall, Ref, parse_pipe, parse_ref
from model_atlas.sources.pathmatch import validate_selector_path

# Columns the engine owns; presets never assign them directly. The temporal bounds come
# from a `time:` block, the lat/lon source fields are auto-captured, ``input_file_path`` /
# ``preset_id`` / ``preset_name`` come from the run + matched preset, ``source_record_uid``
# from the top-level `source_record_uid:` key (or is generated), ``row_uid`` is generated
# per output row, and the derived columns come from untangle.
# ``raw_source_path`` and ``input_record_id`` are intentionally NOT engine-owned: a preset maps
# them (e.g. AXIOM Source / Location), and the engine only fills a default when it does not.
_ENGINE_OWNED_FIELDS: frozenset[str] = frozenset({
    "time_lower_raw", "time_lower_source_field", "time_lower_unix_utc_us",
    "time_upper_raw", "time_upper_source_field", "time_upper_unix_utc_us",
    "latitude_source_field", "longitude_source_field",
    "input_file_path", "source_record_uid", "row_uid", "preset_id", "preset_name",
    "record_type", "record_rank",
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

# The three `links:` keys map to these model columns.
_LINK_FIELDS: dict[str, str] = {
    "entity_position": "entity_position_link",
    "entity_time": "entity_time_link",
    "spatial_temporal": "spatial_temporal_link",
}

_TIME_STRUCTURAL_KEYS = frozenset({"instant", "interval", "epoch", "format", "zone"})
_EPOCHS = frozenset({"unix_s", "unix_ms", "unix_us", "unix_ns", "cocoa", "webkit"})


# --- typed structures -------------------------------------------------------

@dataclass(frozen=True)
class ParserInfo:
    """Kept for traceability compatibility; derived from the preset id + version."""

    name: str
    version: str


@dataclass(frozen=True)
class PresetMeta:
    id: str
    name: str
    version: str
    os: str | None = None
    tool: str | None = None
    os_version: str | None = None
    tier: str | None = None

    @property
    def title(self) -> str:
        prefix = " ".join(part for part in (self.os, self.tool) if part)
        return f"{prefix} — {self.name}" if prefix else self.name


_READER_FORMATS = ("csv", "excel", "sqlite")
_READ_KEYS = ("delimiter", "encoding", "header_row", "skip_rows")


@dataclass(frozen=True)
class InputSelector:
    """One ``input_selector`` entry: where the file is (``path`` XOR ``name``), its
    magic-verified ``format``, an optional ``role``, and the reader params for that
    format (table/sql for sqlite, sheet for excel, delimiter/encoding/… for csv)."""

    format: str                      # csv | excel | sqlite
    role: str = "source"
    path: str | None = None          # anchored, prefix-tolerant; XOR name
    name: str | None = None          # basename anywhere; XOR path
    table: str | None = None         # sqlite
    sql: str | None = None           # sqlite
    sheet: str | None = None         # excel
    read: dict[str, Any] = field(default_factory=dict)

    def reader_params(self) -> dict[str, Any]:
        if self.format == "csv":
            return dict(self.read)
        if self.format == "excel":
            return {"sheet": self.sheet, **self.read}
        params: dict[str, Any] = {}
        if self.table:
            params["table"] = self.table
        if self.sql:
            params["sql"] = self.sql
        return params


@dataclass(frozen=True)
class FieldSpec:
    """How one model field is populated: a typed reference plus optional attributes."""

    model_field: str
    ref: Ref
    type: str | None = None                       # explicit cast override
    unit: str | None = None                       # source unit -> canonical
    extract: tuple[str, str] | None = None        # (pattern_name, group_name)
    pipe: tuple[PipeCall, ...] = ()

    # --- v2-compat accessors used by reporting._column_refs ---
    @property
    def column(self) -> str | None:
        return self.ref.arg if self.ref.kind == "column" else None

    @property
    def from_name_pattern(self) -> str | None:
        return self.ref.arg if self.ref.kind == "header" else None


@dataclass(frozen=True)
class TimeSpec:
    kind: str                       # "instant" | "interval"
    lower: Ref
    upper: Ref
    epoch: str | None = None
    format: str | None = None
    zone: FieldSpec | None = None   # const or header(...) -> utc_offset_hours, applied to parsing
    overrides: tuple[FieldSpec, ...] = ()  # other temporal model fields (accuracy, ...)

    # compat for reporting._column_refs (it reads lower_column/upper_column)
    @property
    def lower_column(self) -> str | None:
        return self.lower.arg if self.lower.kind in ("column", "header") else None

    @property
    def upper_column(self) -> str | None:
        return self.upper.arg if self.upper.kind in ("column", "header") else None


@dataclass(frozen=True)
class Links:
    entity_position: str | None = None
    entity_time: str | None = None
    spatial_temporal: str | None = None

    def as_field_specs(self) -> tuple[FieldSpec, ...]:
        out: list[FieldSpec] = []
        for key, model_field in _LINK_FIELDS.items():
            value = getattr(self, key)
            if value is not None:
                out.append(FieldSpec(model_field=model_field, ref=Ref(kind="const", arg=value)))
        return tuple(out)


@dataclass(frozen=True)
class AssertionTemplate:
    fields: tuple[FieldSpec, ...]   # from `position:` + the `links:` constants
    temporal: tuple[TimeSpec, ...]  # always length 1 in v3 (one time per assertion)


@dataclass(frozen=True)
class PresetSpec:
    meta: PresetMeta
    input_selectors: tuple[InputSelector, ...]
    path: Path
    source_record_uid: FieldSpec | None = None
    common: tuple[FieldSpec, ...] = ()
    assertions: tuple[AssertionTemplate, ...] = ()
    lookup_tables: dict[str, dict[Any, Any]] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)
    # The full source-column inventory the examiner declares before mapping. Drives the
    # drift/frontier report and the AXIOM differential; the mapping maps a subset of it.
    expected_columns: tuple[str, ...] = ()

    # --- compatibility accessors so adapters / matcher / reporting / pipeline
    # keep reading familiar attributes without each knowing the v3 layout. ---
    @property
    def name(self) -> str:
        return self.meta.title

    @property
    def source_tier(self) -> str | None:
        return self.meta.tier

    @property
    def parser(self) -> ParserInfo:
        return ParserInfo(name=self.meta.id, version=self.meta.version)

    @property
    def roles(self) -> tuple[str, ...]:
        """Distinct roles in declaration order. Same role = OR; different roles = AND."""
        seen: list[str] = []
        for selector in self.input_selectors:
            if selector.role not in seen:
                seen.append(selector.role)
        return tuple(seen)


# --- parsing ----------------------------------------------------------------

def _require_str(raw: dict[str, Any], key: str, path: Path, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {where} requires a non-empty string {key!r}")
    return value.strip()


def _validate_enum(model_field: str, value: Any, path: Path) -> None:
    allowed = _ENUM_FIELDS.get(model_field)
    if allowed is not None and value is not None and value not in allowed:
        raise ValueError(f"{path}: {model_field!r} value {value!r} is not one of {sorted(allowed)}")


def _parse_extract(raw: Any, patterns: dict[str, str], path: Path) -> tuple[str, str]:
    if not isinstance(raw, str) or "." not in raw:
        raise ValueError(f"{path}: extract must be 'pattern.group', got {raw!r}")
    pattern_name, _, group = raw.partition(".")
    if pattern_name not in patterns:
        raise ValueError(f"{path}: extract references unknown pattern {pattern_name!r}")
    compiled = re.compile(patterns[pattern_name])
    if group not in compiled.groupindex:
        raise ValueError(
            f"{path}: pattern {pattern_name!r} has no named group {group!r} "
            f"(named groups: {sorted(compiled.groupindex)})"
        )
    return pattern_name, group


def _parse_field(
    model_field: str, raw: Any, path: Path, patterns: dict[str, str], *, assignable: bool = True
) -> FieldSpec:
    if assignable and model_field not in ASSIGNABLE_FIELDS:
        raise ValueError(f"{path}: {model_field!r} is not an assignable model field")
    if isinstance(raw, dict):
        if "from" not in raw:
            raise ValueError(f"{path}: {model_field!r} mapping must set 'from' (a reference)")
        ref = parse_ref(raw["from"])
        extract = _parse_extract(raw["extract"], patterns, path) if "extract" in raw else None
        spec = FieldSpec(
            model_field=model_field,
            ref=ref,
            type=raw.get("type"),
            unit=raw.get("unit"),
            extract=extract,
            pipe=parse_pipe(raw.get("pipe")),
        )
    else:
        spec = FieldSpec(model_field=model_field, ref=parse_ref(raw))
    if spec.ref.kind == "const":
        _validate_enum(model_field, spec.ref.arg, path)
    return spec


def _parse_links(raw: Any, path: Path) -> Links:
    if raw is None:
        return Links()
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: links must be a mapping")
    unknown = set(raw) - set(_LINK_FIELDS)
    if unknown:
        raise ValueError(f"{path}: unknown links key(s) {sorted(unknown)}; expected {sorted(_LINK_FIELDS)}")
    for key, model_field in _LINK_FIELDS.items():
        if key in raw:
            _validate_enum(model_field, raw[key], path)
    return Links(**{key: raw.get(key) for key in _LINK_FIELDS})


def _parse_time(raw: Any, path: Path, patterns: dict[str, str]) -> TimeSpec:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 'time' must be a mapping")
    has_instant = "instant" in raw
    has_interval = "interval" in raw
    if has_instant == has_interval:
        raise ValueError(f"{path}: time needs exactly one of 'instant' or 'interval'")
    if raw.get("epoch") is not None and raw.get("format") is not None:
        raise ValueError(f"{path}: time sets both 'epoch' and 'format'; choose one")
    epoch = raw.get("epoch")
    if epoch is not None and epoch not in _EPOCHS:
        raise ValueError(f"{path}: unknown epoch {epoch!r}; expected one of {sorted(_EPOCHS)}")
    if has_instant:
        ref = parse_ref(raw["instant"])
        lower = upper = ref
    else:
        interval = raw["interval"]
        if not isinstance(interval, dict) or "lower" not in interval or "upper" not in interval:
            raise ValueError(f"{path}: interval needs 'lower' and 'upper'")
        lower, upper = parse_ref(interval["lower"]), parse_ref(interval["upper"])
    zone = _parse_field("utc_offset_hours", raw["zone"], path, patterns) if "zone" in raw else None
    overrides = tuple(
        _parse_field(key, value, path, patterns)
        for key, value in raw.items()
        if key not in _TIME_STRUCTURAL_KEYS
    )
    return TimeSpec(
        kind="instant" if has_instant else "interval",
        lower=lower, upper=upper, epoch=epoch, format=raw.get("format"),
        zone=zone, overrides=overrides,
    )


def _parse_assertion(raw: Any, path: Path, patterns: dict[str, str]) -> AssertionTemplate:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each assertion must be a mapping")
    if "time" not in raw:
        raise ValueError(f"{path}: each assertion must declare 'time'")
    position = raw.get("position") or {}
    if not isinstance(position, dict):
        raise ValueError(f"{path}: 'position' must be a mapping")
    fields = [_parse_field(key, value, path, patterns) for key, value in position.items()]
    fields.extend(_parse_links(raw.get("links"), path).as_field_specs())
    time = _parse_time(raw["time"], path, patterns)
    return AssertionTemplate(fields=tuple(fields), temporal=(time,))


def _parse_one_selector(raw: Any, path: Path) -> InputSelector:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: each input_selector entry must be a mapping")
    fmt = raw.get("format")
    if fmt not in _READER_FORMATS:
        raise ValueError(f"{path}: no reader for format {fmt!r}; expected one of {list(_READER_FORMATS)}")
    has_path, has_name = raw.get("path") is not None, raw.get("name") is not None
    if has_path == has_name:
        raise ValueError(f"{path}: input_selector needs exactly one of 'path' or 'name'")
    if has_path:
        validate_selector_path(str(raw["path"]))
    if fmt == "sqlite":
        if bool(raw.get("table")) == bool(raw.get("sql")):
            raise ValueError(f"{path}: a sqlite input_selector needs exactly one of 'table' or 'sql'")
    if fmt == "excel" and not raw.get("sheet"):
        raise ValueError(f"{path}: an excel input_selector needs 'sheet'")
    read = {k: raw[k] for k in _READ_KEYS if k in raw}
    role = raw.get("role") or "source"
    return InputSelector(
        format=fmt, role=str(role),
        path=raw.get("path"), name=raw.get("name"),
        table=raw.get("table"), sql=raw.get("sql"), sheet=raw.get("sheet"),
        read=read,
    )


def _parse_input_selectors(raw: Any, path: Path) -> tuple[InputSelector, ...]:
    if raw is None:
        raise ValueError(f"{path}: 'input_selector' is required")
    entries = raw if isinstance(raw, list) else [raw]
    if not entries:
        raise ValueError(f"{path}: 'input_selector' must not be empty")
    return tuple(_parse_one_selector(entry, path) for entry in entries)


def _parse_meta(raw: Any, path: Path) -> PresetMeta:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 'preset' header must be a mapping")
    tier = raw.get("tier")
    if tier is not None:
        _validate_enum("source_tier", tier, path)
    version = raw.get("version")
    return PresetMeta(
        id=_require_str(raw, "id", path, "preset"),
        name=_require_str(raw, "name", path, "preset"),
        version=str(version) if version is not None else "0",
        os=(raw.get("os") or None),
        tool=(raw.get("tool") or None),
        os_version=(raw.get("os_version") or None),
        tier=tier,
    )


def preset_spec_from_yaml(raw_obj: object, path: Path) -> PresetSpec:
    if not isinstance(raw_obj, dict):
        raise ValueError(f"{path}: preset must be a YAML mapping")
    raw = raw_obj

    meta = _parse_meta(raw.get("preset"), path)
    input_selectors = _parse_input_selectors(raw.get("input_selector"), path)

    patterns = raw.get("patterns") or {}
    if not isinstance(patterns, dict):
        raise ValueError(f"{path}: 'patterns' must be a mapping")
    patterns = {str(k): str(v) for k, v in patterns.items()}
    for name, pattern in patterns.items():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{path}: pattern {name!r} is not a valid regex: {exc}") from exc

    lookup_tables = raw.get("lookup_tables") or {}
    if not isinstance(lookup_tables, dict) or not all(isinstance(v, dict) for v in lookup_tables.values()):
        raise ValueError(f"{path}: 'lookup_tables' must be a mapping of name -> table")

    expected = raw.get("expected_columns") or []
    if not isinstance(expected, list) or not all(isinstance(c, str) for c in expected):
        raise ValueError(f"{path}: 'expected_columns' must be a list of column-name strings")

    source_record_uid_raw = raw.get("source_record_uid")
    source_record_uid = (
        _parse_field("source_record_uid", source_record_uid_raw, path, patterns, assignable=False)
        if source_record_uid_raw is not None else None
    )

    common_raw = raw.get("common") or {}
    if not isinstance(common_raw, dict):
        raise ValueError(f"{path}: 'common' must be a mapping")
    common = tuple(_parse_field(key, value, path, patterns) for key, value in common_raw.items())

    assertions_raw = raw.get("assertions")
    if not isinstance(assertions_raw, list) or not assertions_raw:
        raise ValueError(f"{path}: 'assertions' must be a non-empty list")
    assertions = tuple(_parse_assertion(entry, path, patterns) for entry in assertions_raw)

    return PresetSpec(
        meta=meta, input_selectors=input_selectors, path=path,
        source_record_uid=source_record_uid, common=common, assertions=assertions,
        lookup_tables={str(k): dict(v) for k, v in lookup_tables.items()},
        patterns=patterns,
        expected_columns=tuple(expected),
    )
