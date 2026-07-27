"""Declarative preset schema (v3) for the pluggable pipeline.

Defines:    the typed preset structures (PresetMeta, InputSelector, FieldSpec, TimeSpec,
            Links, AssertionTemplate, PresetSpec) and preset_spec_from_yaml(), which
            parses + validates a v3 YAML preset against the canonical model.
Used by:    the loader, the assembly engine, the matcher, and reporting.
Depends on: core.vocabularies (the link/tier/state enums), presets.expr (ref + pipe
            parser), ingest.pathmatch (selector path validation).

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
    source_record_uid: field(ArtifactID)   # optional; omit to auto-generate a deterministic per-record UID
    raw_source_path: preset(path)          # where the trace came from (mapped in `common:`)
    lookup_tables: {recovery: {Parsing: intact}}
    patterns: {coords: "(?P<lat>...) - (?P<lon>...)"}
    common:
      entity: const(device)
    assertions:
      - position: {latitude_wgs84: field(ZLATITUDE), ...}
        time:     {instant: field(ZTIMESTAMP), epoch: cocoa, zone: UTC}
        links:    {entity_position: at, entity_time: observed_at, spatial_temporal: instant}

A value is ONE explicit reference call (field/header/filename/param/const/format), an
``if / elif / else`` conditional chain over such calls (decision 33), or a mapping
``{from: <ref-or-conditional>, type:, unit:, extract:, pipe:}``. See presets.expr.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from model_atlas.core.vocabularies import (
    EntityPositionLink,
    EntityTimeLink,
    RecordState,
    SourceTier,
    SpatialTemporalLink,
)
from model_atlas.ingest.pathmatch import validate_selector_path
from model_atlas.presets.expr import (
    Cond,
    PipeCall,
    Ref,
    is_condition,
    parse_cond,
    parse_pipe,
    parse_ref,
)

# The model fields a preset may assign — the historic 45-column flat vocabulary MINUS
# the engine-owned columns (temporal bounds come from a `time:` block, lat/lon source
# fields are auto-captured, ``input_file_path`` / ``preset_id`` / ``preset_name`` came
# from the run + matched preset, ``source_record_uid`` from its top-level key,
# ``row_uid`` was generated per output row, and record_type/record_rank come from
# untangle). ``raw_source_path`` and ``input_record_id`` stay assignable: a preset maps
# them (e.g. AXIOM Source / Location) and the engine only fills a default when it does not.
# T60: formerly derived from ``model.families.OUTPUT_COLUMNS``; with the flat model
# retired, the preset language's target vocabulary is declared HERE, once. The extended L1
# targets (entity_role, activity_kind, context.<key>, ...) that ingest.assemble consumes are
# admitted here too, so a real YAML preset can map them (not only the programmatic tests).

# Keys under this prefix become entries in the activity's context object (one per party /
# attribute the record mentions without positioning) — the key after the dot is free text,
# so the assignable gate matches the PREFIX rather than a fixed name.
CONTEXT_FIELD_PREFIX = "context."

ASSIGNABLE_FIELDS: frozenset[str] = frozenset({
    # entity
    "entity", "entity_type", "linked_entity", "entity_role", "identifier_type",
    # temporal (assignable subset). time_accuracy_us_lower / _upper are the source-stated
    # per-bound timing uncertainties (µs) — the temporal analogue of the spatial corner's
    # accuracy, split per bound like temporal_role_lower/_upper: ingest.assemble writes each
    # as a verbatim `<side>_accuracy` component and integrate.temporal reads it through to
    # timespan_integration.<side>_accuracy_us, None when unmapped (never invented).
    "utc_offset_hours", "temporal_source", "temporal_referential",
    "temporal_role_lower", "temporal_role_upper",
    "temporal_part_lower", "temporal_part_upper",
    "time_accuracy_us_lower", "time_accuracy_us_upper",
    # spatial
    "latitude_wgs84", "longitude_wgs84", "altitude_m", "position_type", "raw_position",
    "position_source", "horizontal_accuracy_m", "vertical_accuracy_m",
    "horizontal_speed_kmh", "vertical_speed_kmh", "horizontal_speed_accuracy_kmh",
    "vertical_speed_accuracy_kmh", "heading_deg", "heading_accuracy_deg",
    "beam_azimuth_deg", "beam_width_deg", "spatial_referential",
    # activity
    "activity_kind", "multiplicity",
    # bindings
    "entity_position_link", "entity_time_link", "spatial_temporal_link",
    # provenance (assignable subset)
    "raw_source_path", "data_source", "input_record_id", "source_label", "source_tier",
    "deleted",
})


def _is_assignable(model_field: str) -> bool:
    """Whether a preset may map ``model_field``: a listed target, or a ``context.<key>``
    (whose free key names one entry of the activity context object)."""
    return model_field in ASSIGNABLE_FIELDS or model_field.startswith(CONTEXT_FIELD_PREFIX)

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


_READER_FORMATS = ("csv", "excel", "sqlite", "xml")
_READ_KEYS = ("delimiter", "encoding", "header_row", "skip_rows")


@dataclass(frozen=True)
class InputSelector:
    """One ``input_selector`` entry: where the file is (``path`` XOR ``name``), its
    magic-verified ``format``, an optional ``role``, and the reader params for that
    format (table/sql for sqlite, sheet for excel, namespace/model for xml,
    delimiter/encoding/… for csv)."""

    format: str                      # csv | excel | sqlite | xml
    role: str = "source"
    path: str | None = None          # anchored, prefix-tolerant; XOR name
    name: str | None = None          # basename anywhere; XOR path
    table: str | None = None         # sqlite
    sql: str | None = None           # sqlite
    sheet: str | None = None         # excel
    namespace: str | None = None     # xml — the dialect's root namespace (discriminator)
    model: str | None = None         # xml — the model type to extract
    read: dict[str, Any] = field(default_factory=dict)

    def reader_params(self) -> dict[str, Any]:
        if self.format == "csv":
            return dict(self.read)
        if self.format == "excel":
            return {"sheet": self.sheet, **self.read}
        if self.format == "xml":
            return {"namespace": self.namespace, "model": self.model}
        params: dict[str, Any] = {}
        if self.table:
            params["table"] = self.table
        if self.sql:
            params["sql"] = self.sql
        return params


@dataclass(frozen=True)
class FieldSpec:
    """How one model field is populated: either a single typed reference (``ref``) OR a
    conditional chain (``condition``, decision 33) — exactly one is set — plus optional
    attributes. When a ``condition`` is set, ``ref`` is None; the compat accessors below
    then report None (a conditional is not a single field/header reference)."""

    model_field: str
    ref: Ref | None = None                         # a single ref; None when ``condition`` set
    condition: Cond | None = None                  # an if/elif/else chain; None for a plain ref
    type: str | None = None                       # explicit cast override
    unit: str | None = None                       # source unit -> canonical
    extract: tuple[str, str] | None = None        # (pattern_name, group_name)
    pipe: tuple[PipeCall, ...] = ()
    optional: bool = False                         # a mapped field absent => null, not a
                                                   # match failure (e.g. speed before iOS 17)

    # --- v2-compat accessors used by presets.fields.field_refs ---
    @property
    def field(self) -> str | None:
        return self.ref.arg if self.ref is not None and self.ref.kind == "field" else None

    @property
    def from_name_pattern(self) -> str | None:
        return self.ref.arg if self.ref is not None and self.ref.kind == "header" else None


@dataclass(frozen=True)
class TimeSpec:
    kind: str                       # "instant" | "interval"
    lower: Ref
    upper: Ref
    epoch: str | None = None
    format: str | None = None
    zone: FieldSpec | None = None   # const or header(...) -> utc_offset_hours, applied to parsing
    overrides: tuple[FieldSpec, ...] = ()  # other temporal model fields (accuracy, ...)
    # Optional per-bound substring selection (pattern_name, group) — e.g. pulling "14:00"
    # out of a free-text witness statement. A selection of verbatim source characters, so it
    # stays within the L1 no-conversion rule (same as a field's ``extract``).
    lower_extract: tuple[str, str] | None = None
    upper_extract: tuple[str, str] | None = None

    # compat for presets.fields.field_refs (it reads lower_field/upper_field)
    @property
    def lower_field(self) -> str | None:
        return self.lower.arg if self.lower.kind in ("field", "header") else None

    @property
    def upper_field(self) -> str | None:
        return self.upper.arg if self.upper.kind in ("field", "header") else None


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
    # The full source-field inventory the examiner declares before mapping. Drives the
    # drift/frontier report and the AXIOM differential; the mapping maps a subset of it.
    expected_fields: tuple[str, ...] = ()

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
    if assignable and not _is_assignable(model_field):
        raise ValueError(f"{path}: {model_field!r} is not an assignable model field")
    if isinstance(raw, dict):
        if "from" not in raw:
            raise ValueError(f"{path}: {model_field!r} mapping must set 'from' (a reference)")
        # A conditional chain (decision 33) is accepted anywhere a plain ref is: as the bare
        # value or as the dict form's ``from``. The remaining attributes (type/unit/extract/
        # pipe) still apply to whichever branch value wins.
        from_value = raw["from"]
        ref, condition = _parse_ref_or_cond(from_value)
        extract = _parse_extract(raw["extract"], patterns, path) if "extract" in raw else None
        spec = FieldSpec(
            model_field=model_field,
            ref=ref,
            condition=condition,
            type=raw.get("type"),
            unit=raw.get("unit"),
            extract=extract,
            pipe=parse_pipe(raw.get("pipe")),
            optional=bool(raw.get("optional", False)),
        )
    else:
        ref, condition = _parse_ref_or_cond(raw)
        spec = FieldSpec(model_field=model_field, ref=ref, condition=condition)
    # A controlled-vocabulary field must hold a valid value however it is produced: a plain
    # const, or any const RESULT of a conditional (a const test/comparand is a value to
    # compare against, not an assigned value, so it is not enum-checked).
    if spec.ref is not None and spec.ref.kind == "const":
        _validate_enum(model_field, spec.ref.arg, path)
    if spec.condition is not None:
        for result in _condition_result_refs(spec.condition):
            if result.kind == "const":
                _validate_enum(model_field, result.arg, path)
    return spec


def _parse_ref_or_cond(raw: Any) -> tuple[Ref | None, Cond | None]:
    """Parse a mapping value into either a single ``Ref`` or a conditional ``Cond``
    (decision 33). Exactly one of the returned pair is non-None."""
    if is_condition(raw):
        return None, parse_cond(raw)
    return parse_ref(raw), None


def _condition_result_refs(condition: Cond) -> tuple[Ref, ...]:
    """The refs a conditional can ASSIGN (each branch's ``then`` + the ``else_``), used to
    enum-check the produced values without checking the test/comparand refs."""
    results = [branch.then for branch in condition.branches]
    if condition.else_ is not None:
        results.append(condition.else_)
    return tuple(results)


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
        ref, extract = _parse_time_bound(raw["instant"], path, patterns)
        lower = upper = ref
        lower_extract = upper_extract = extract
    else:
        interval = raw["interval"]
        if not isinstance(interval, dict) or "lower" not in interval or "upper" not in interval:
            raise ValueError(f"{path}: interval needs 'lower' and 'upper'")
        lower, lower_extract = _parse_time_bound(interval["lower"], path, patterns)
        upper, upper_extract = _parse_time_bound(interval["upper"], path, patterns)
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
        lower_extract=lower_extract, upper_extract=upper_extract,
    )


def _parse_time_bound(raw: Any, path: Path, patterns: dict[str, str]) -> tuple[Ref, tuple[str, str] | None]:
    """One time bound: a bare reference, or ``{from: <ref>, extract: pattern.group}``.

    The dict form lets a bound pull a substring out of a messy source cell (e.g. a time out
    of a free-text statement), exactly as a field mapping's ``extract`` does — returning the
    parsed reference and the optional (pattern, group) selection."""
    if isinstance(raw, dict):
        if "from" not in raw:
            raise ValueError(f"{path}: a time-bound mapping must set 'from' (a reference)")
        ref = parse_ref(raw["from"])
        extract = _parse_extract(raw["extract"], patterns, path) if "extract" in raw else None
        return ref, extract
    return parse_ref(raw), None


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
    if fmt == "xml" and not raw.get("model"):
        raise ValueError(f"{path}: an xml input_selector needs 'model' (the model type to extract)")
    read = {k: raw[k] for k in _READ_KEYS if k in raw}
    role = raw.get("role") or "source"
    return InputSelector(
        format=fmt, role=str(role),
        path=raw.get("path"), name=raw.get("name"),
        table=raw.get("table"), sql=raw.get("sql"), sheet=raw.get("sheet"),
        namespace=raw.get("namespace"), model=raw.get("model"),
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

    expected = raw.get("expected_fields") or []
    if not isinstance(expected, list) or not all(isinstance(c, str) for c in expected):
        raise ValueError(f"{path}: 'expected_fields' must be a list of field-name strings")

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
        expected_fields=tuple(expected),
    )
