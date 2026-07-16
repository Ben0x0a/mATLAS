"""Assembly engine: build typed six-family assertions from extracted data + a v3 preset.

Defines:    BuildEnv (pipeline-derived defaults), to_records (NaN->None) and build_assertions
            (the fan-out + reference + pipe + raw-capture engine, emitting typed family
            objects into a RunStore and returning its transform warnings + the assertion
            count). Field resolution also evaluates conditional mapping values (decision 33,
            ``_resolve_cond``) and ``format(...)`` templates (decision 42,
            ``_resolve_template``); both yield ``NA_SENTINEL`` when they produce nothing.
Used by:    the pipeline (fills one run-wide store) and the test helper ``tests.support``.
Depends on: model.families (the family dataclasses + columns/types/units), model.store
            (RunStore + the single passthrough-naming helper), presets.spec + expr
            (refs, Cond, Template, NA_SENTINEL) + fields (resolver), transforms.

Phase C (docs/relational_model.md): the pipeline creates ONE RunStore per run and
build_assertions fills it per matched source (registering the ``source`` row with its
record/assertion counts). The record body composes a SpatioTemporalAssertion and adds it
to the store instead of appending a flat dict; the flat working dict is kept only as the
mutable holder the resolution logic writes into and the frozen families are built from at
the end. row_uid / source_record_uid are byte-identical to before.

One source row x each assertion (each with one time spec) -> one flat row. Temporal
raw values and source fields, and the lat/lon source fields, are captured
automatically so an author can never forget the value<->provenance pairing.
``source_record_uid`` is a UID shared by every output row of one source record: the tool's
own uid when a preset maps ``source_record_uid`` (so the rows link back to the tool
artefact), else a deterministic uuid5 over the source content fingerprint + path + the
physical source line number — globally unique and portable; a
mapped one is guarded for uniqueness. The 1-based source line number is carried by
``input_record_id`` (its default ``<table>#<line>``) and used internally for the UIDs.
``row_uid`` is generated per OUTPUT row (deterministic uuid5 over the output row's MODEL
data + the source line number + the output ordinal, scoped by the source identity):
content-addressed, yet the line number keeps identical-data records distinct and the output
ordinal keeps a record's fan-out rows distinct.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import model_atlas.transforms.builtin  # noqa: F401 - registers builtins
from model_atlas.model.families import (
    OUTPUT_COLUMNS,
    UTC_OFFSET_UNKNOWN,
    Bindings,
    Derived,
    Entity,
    Provenance,
    Spatial,
    SpatioTemporalAssertion,
    Temporal,
    TemporalBound,
    column_cast,
    unit_factor,
)
from model_atlas.model.store import (
    AssertionRow,
    RecordRow,
    RunStore,
    SourceRow,
)
from model_atlas.presets.expr import (
    NA_LITERAL,
    NA_SENTINEL,
    Cond,
    Ref,
    Template,
    parse_param_expr,
)
from model_atlas.presets.fields import make_resolver
from model_atlas.presets.spec import FieldSpec, InputSelector, PresetSpec, TimeSpec
from model_atlas.transforms.builtin import (
    ZoneToken,
    datetime_to_us,
    epoch_to_us,
    local_naive_to_utc_us,
    parse_datetime_to_us,
    parse_iso8601,
    parse_zone_token,
    zone_offset_hours_at,
    zone_standard_offset_hours,
)

# Recognised time codec for ISO-8601 strings (e.g. Cellebrite TimeStamp): zone-qualified
# values parse with their own offset; naive values are resolved via the preset ``zone:``.
ISO8601_FORMAT = "iso8601"
from model_atlas.transforms.registry import PipeContext, run_pipe

# Fixed namespaces so deterministic UIDs are stable across runs/machines.
_SOURCE_ROW_NAMESPACE = uuid.UUID("6f9b1d2e-7c3a-5e41-9a2b-0c8d4e1f2a3b")
_OUTPUT_ROW_NAMESPACE = uuid.UUID("b3a0c1d2-4e5f-5a6b-8c9d-0e1f2a3b4c5d")
# Namespace for the per-source id (fingerprint|raw_source_path|preset_id). Not projected to
# the flat frame this phase; it keys the SourceRow so records can point back to their source.
_SOURCE_NAMESPACE = uuid.UUID("2d1c0b3a-6e5f-5c4b-9a8d-1f0e3a2b5c4d")

# Who asserted the entity->linked_entity attribution: the operator (a run-level
# --linked-entity) or a preset mapping. Stored on AssertionRow, never in the flat frame.
ATTRIBUTION_OPERATOR = "operator_param"
ATTRIBUTION_PRESET = "preset_mapped"

# Columns NOT folded into the row_uid content digest: machine-specific paths, the ids
# themselves (circular), run/preset metadata, and untangle-computed ranks. What remains is
# the assertion's evidential content (entity/temporal/spatial/bindings/deleted), so row_uid
# is content-addressed yet portable (the SAME db read from a folder or a zip is identical).
_NON_CONTENT_COLUMNS: frozenset[str] = frozenset({
    "raw_source_path", "input_file_path", "input_record_id",
    "source_record_uid", "row_uid", "preset_id", "preset_name",
    "source_label", "source_tier", "record_type", "record_rank",
})
_ROW_UID_CONTENT_COLUMNS: tuple[str, ...] = tuple(c for c in OUTPUT_COLUMNS if c not in _NON_CONTENT_COLUMNS)


@dataclass(frozen=True)
class BuildEnv:
    """Pipeline/operator-supplied values for one source. ``input_file_path`` (full path of
    the outermost on-disk artifact matlas opened) and ``source_fingerprint`` (the source's
    content hash) are engine-set; ``input_file_name`` is its basename (backs ``filename()``
    fallback); ``raw_source_path`` is the engine default for the trace origin when the
    preset maps none; ``source_tier`` is a default the preset may override; entity/
    linked_entity OVERRIDE the preset. ``source_file_path`` backs the ``filename(path)`` ref."""

    input_file_path: str | None = None
    input_file_name: str | None = None
    source_fingerprint: str | None = None
    source_file_path: str | None = None
    raw_source_path: str | None = None
    source_tier: str | None = None
    entity: str | None = None
    linked_entity: str | None = None
    source_file_name: str | None = None
    # The configured local IANA zone (matlas_config.toml). For an absolute-UTC (epoch)
    # source with no preset-declared zone, the engine records this zone's DST-aware offset
    # at each row's instant in utc_offset_hours. None => unknown zone => null offset.
    local_zone: str | None = None


def _source_container(selector: "InputSelector | None") -> str | None:
    """The table / sheet / query the rows were read from — the prefix of input_record_id."""
    if selector is None:
        return None
    return selector.table or selector.sheet or ("query" if selector.sql else None)


def _preset_values(preset: PresetSpec, selector: "InputSelector | None") -> dict[str, Any]:
    """Values a ``preset(<key>)`` ref may read from the matched selector + preset meta."""
    meta = preset.meta
    values: dict[str, Any] = {
        "id": meta.id, "name": meta.title, "tier": meta.tier, "os": meta.os,
        "tool": meta.tool, "version": meta.version, "os_version": meta.os_version,
    }
    if selector is not None:
        values.update({
            "path": selector.path, "name": selector.name, "table": selector.table,
            "sheet": selector.sheet, "sql": selector.sql, "format": selector.format,
        })
    return values


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def to_records(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    """Row dicts with every missing/NaN cell normalised to None."""
    return [
        {column: (None if _is_missing(value) else value) for column, value in row.items()}
        for row in dataframe.to_dict(orient="records")
    ]


def _coerce_scalar(value: Any, target: str) -> Any:
    if target == "int":
        return int(value)
    if target == "float":
        return float(value)
    if target == "str":
        return str(value)
    if target == "bool":
        return bool(value)
    raise ValueError(f"unsupported cast target {target!r}")


def _resolve_ref(
    ref: Ref,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: BuildEnv,
    preset_values: dict[str, Any],
) -> tuple[Any, str | None]:
    """Return (value, source_field) for a reference. source_field is set only when
    the value originates from a named source field (field/header), for provenance."""
    if ref.kind == "const":
        return ref.arg, None
    if ref.kind == "format":
        return _resolve_template(ref.arg, row, resolve, file_values, env, preset_values), None
    if ref.kind == "param":
        return (env.entity if ref.arg == "entity" else env.linked_entity), None
    if ref.kind == "filename":
        return file_values.get(ref.arg), None
    if ref.kind == "preset":
        return preset_values.get(ref.arg), None
    if ref.kind == "header":
        col = resolve(ref.arg)
        return col, col  # the value IS the matched field's header text
    col = resolve(ref.arg)  # field
    return (row.get(col) if col is not None else None), col


def _resolve_template(
    template: Template,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: BuildEnv,
    preset_values: dict[str, Any],
) -> Any:
    """Render a ``format(...)`` template (decision 42) for one row. Literal segments are kept
    verbatim; each placeholder ref is resolved and a missing/NA value renders as EMPTY text,
    so the connecting literals still appear. If EVERY placeholder is missing the whole value
    is ``NA_SENTINEL`` (the same "produced nothing" marker as a conditional with no match) —
    NOT an all-connectors string; a template with at least one present placeholder joins."""
    parts: list[str] = []
    has_placeholder = False
    any_present = False
    for segment in template.segments:
        if isinstance(segment, Ref):
            has_placeholder = True
            value, _ = _resolve_ref(segment, row, resolve, file_values, env, preset_values)
            if _is_missing(value):
                parts.append("")   # a missing placeholder collapses to empty; connectors stay
            else:
                any_present = True
                parts.append(str(value))
        else:
            parts.append(segment)
    if has_placeholder and not any_present:
        return NA_SENTINEL
    return "".join(parts)


def _resolve_param(
    ref: Ref | None,
    literal: str | None,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: BuildEnv,
    preset_values: dict[str, Any],
) -> str | None:
    """Resolve a run-level param for one row. ``ref`` (from ``parse_param_expr``) resolves
    per row when set; otherwise the plain ``literal`` is used verbatim. Non-null values are
    stringified so the entity / linked_entity columns stay textual."""
    if ref is None:
        return literal
    value, _ = _resolve_ref(ref, row, resolve, file_values, env, preset_values)
    return None if value is None else str(value)


def _resolve_cond(
    cond: Cond,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: BuildEnv,
    preset_values: dict[str, Any],
) -> tuple[Any, str | None]:
    """Evaluate a conditional mapping value (decision 33). Branches are tested top-to-bottom
    and the FIRST match wins; its ``then`` ref is resolved and returned. With no match and no
    ``else`` the result is ``NA_SENTINEL`` — a distinct value, NOT None (None means the field
    was never mapped; NA_SENTINEL means the conditional ran and nothing matched)."""
    for branch in cond.branches:
        test_value, _ = _resolve_ref(branch.test, row, resolve, file_values, env, preset_values)
        comparand_value, _ = _resolve_ref(branch.comparand, row, resolve, file_values, env, preset_values)
        if _cond_match(test_value, branch.op, branch.comparand, comparand_value):
            return _resolve_ref(branch.then, row, resolve, file_values, env, preset_values)
    if cond.else_ is not None:
        return _resolve_ref(cond.else_, row, resolve, file_values, env, preset_values)
    return NA_SENTINEL, None


def _cond_match(test_value: Any, op: str, comparand_ref: Ref, comparand_value: Any) -> bool:
    """NA-aware comparison for one conditional branch. A ``const(NA)`` comparand is the
    explicit missing-value test (``field(X) == "NA"`` means "X is absent"); otherwise values
    are compared for equality, tolerating source type drift (a CSV "1" vs a numeric 1) by
    falling back to a string comparison. Only ``==``/``!=`` are supported."""
    # WHY the const(NA) special case: "NA" is not a value that lives in the source; the owner
    # made it the spelling of the missing test, so it must consult _is_missing, not compare
    # the literal string "NA" against the cell.
    if comparand_ref.kind == "const" and comparand_ref.arg == NA_LITERAL:
        missing = _is_missing(test_value)
        return missing if op == "==" else not missing
    if _is_missing(test_value) or _is_missing(comparand_value):
        equal = _is_missing(test_value) and _is_missing(comparand_value)
    else:
        # Direct equality first (int == int); fall back to textual equality so a source
        # value typed differently from the literal (CSV "1" vs const 1) still matches.
        equal = (test_value == comparand_value) or (str(test_value) == str(comparand_value))
    return equal if op == "==" else not equal


def _resolve_field(
    spec: FieldSpec,
    row: dict[str, Any],
    warnings: list[str],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: BuildEnv,
    ctx: PipeContext,
    preset_values: dict[str, Any],
) -> tuple[Any, str | None]:
    # A conditional (decision 33) resolves to its first-matching branch / else / NA_SENTINEL;
    # a plain ref (including a format(...) template, decision 42) resolves directly. Either may
    # yield NA_SENTINEL ("produced nothing" — a conditional with no match, or a template whose
    # every placeholder was missing); the sentinel short-circuits the transform tail below,
    # being a terminal marker, not a value to extract/pipe/unit/cast. A real value (a matched
    # branch, or a joined template) flows through the SAME tail as any ref, so type/unit/
    # extract/pipe still apply to it.
    if spec.condition is not None:
        value, source_col = _resolve_cond(spec.condition, row, resolve, file_values, env, preset_values)
    else:
        value, source_col = _resolve_ref(spec.ref, row, resolve, file_values, env, preset_values)
    if value is NA_SENTINEL:
        return value, source_col
    if spec.extract is not None and value is not None:
        pattern_name, group = spec.extract
        match = re.search(ctx.patterns[pattern_name], str(value))
        value = match.group(group) if match else None
    if spec.pipe:
        value = run_pipe(value, spec.pipe, ctx, warnings)
    if value is not None and spec.unit:
        value = float(value) * unit_factor(spec.unit, spec.model_field)
    if value is not None:
        inferred = column_cast(spec.model_field)
        target = spec.type or (inferred.__name__ if inferred is not None else None)
        if target is not None:
            value = _coerce_scalar(value, target)
    return value, source_col


def _source_record_uid(
    preset: PresetSpec, base: dict[str, Any], row: dict[str, Any], warnings: list[str],
    resolve: Callable[[str | None], str | None], file_values: dict[str, Any], env: BuildEnv,
    ctx: PipeContext, preset_values: dict[str, Any], source_line_number: int,
) -> tuple[str, bool]:
    """The per-source-record UID, shared by every output row derived from this record, and
    whether it was preset-mapped. A preset that maps ``source_record_uid`` supplies the
    tool's own value verbatim (so the rows link back to the tool artefact); otherwise
    generate a deterministic uuid5 over the source content fingerprint + path + the physical
    source line number — unique regardless of row data, and identical for the same db read
    from a folder vs a zip (folder == zip parity), portable across machines."""
    if preset.source_record_uid is not None:
        resolved, _ = _resolve_field(preset.source_record_uid, row, warnings, resolve, file_values, env, ctx, preset_values)
        return str(resolved), True
    # Content-addressed by physical position: fingerprint + origin path + source line number.
    # Deliberately NOT the input_file name/path (those differ for a folder vs a zip) and NOT
    # the row data or the (possibly preset-mapped, possibly non-unique) input_record_id VALUE
    # — the physical line number is always unique within the source, so identical-data rows
    # never collide.
    identity = "|".join(str(part) for part in (
        env.source_fingerprint, base.get("raw_source_path"), source_line_number,
    ))
    return str(uuid.uuid5(_SOURCE_ROW_NAMESPACE, identity)), False


def _decode_time(
    value: Any, spec: TimeSpec, zone_token: "ZoneToken | None", local_zone: str | None,
    warnings: list[str],
) -> tuple[int | None, float | None]:
    """Decode one bound to (unix_utc_us, source_offset_hours). The unix is always absolute
    UTC; source_offset is the offset actually used to reach it (None for epoch/unknown).

    Source-zone modes (see the proposal):
    - epoch                          -> already UTC.
    - format + ``%z``                -> the value carries its own offset.
    - format + fixed offset (no DST) -> subtract the fixed offset.
    - format + ``[DST]``/``local``   -> resolve the naive local value in ``local_zone`` via
      zoneinfo (per-row, DST-aware), warning on a base/zone mismatch and on the DST
      overlap (ambiguous) / gap (imaginary) hours. No ``local_zone`` -> warn + base offset."""
    if value is None:
        return None, None
    if spec.epoch is not None:
        return epoch_to_us(value, spec.epoch), None
    if spec.format is not None:
        # Parse once into a datetime (aware or naive); ISO-8601 self-indicates its zone, a
        # strptime format follows its %z. The downstream aware/naive handling is shared.
        if spec.format == ISO8601_FORMAT:
            naive = parse_iso8601(value)
            if naive is None:
                return None, None
        else:
            naive = dt.datetime.strptime(str(value), spec.format)
        if naive.tzinfo is not None:                          # the value carries its own offset
            return datetime_to_us(naive), naive.utcoffset().total_seconds() / 3600
        dst = bool(zone_token and zone_token.dst)
        base = zone_token.base_offset_hours if zone_token else None
        if dst and local_zone:
            std = zone_standard_offset_hours(local_zone)
            if base is not None and abs(std - base) > 1e-9:
                warnings.append(
                    f"time-zone header base offset {base}h does not match configured local_zone "
                    f"{local_zone!r} standard offset {std}h; check the zone")
            unix_us, off, anomaly = local_naive_to_utc_us(naive, local_zone)
            if anomaly == "ambiguous":
                warnings.append(
                    f"ambiguous local time {value!r} in {local_zone} (DST fall-back overlap); "
                    f"recorded as the earlier instant")
            elif anomaly == "imaginary":
                warnings.append(
                    f"nonexistent local time {value!r} in {local_zone} (DST spring-forward gap); "
                    f"check the zone")
            return unix_us, off
        if dst and not local_zone:
            warnings.append(
                f"DST/local timestamp {value!r} but no local_zone configured; using base offset "
                f"{base if base is not None else 0.0}h — DST-period rows may be off by an hour")
        off = base if base is not None else 0.0
        aware = naive.replace(tzinfo=dt.timezone(dt.timedelta(hours=off)))
        # base known -> a real offset; base unknown -> we ASSUMED UTC to materialise the
        # instant, so flag the offset as unknown rather than report a misleading null/0.0.
        return datetime_to_us(aware), (base if base is not None else UTC_OFFSET_UNKNOWN)
    # Neither epoch nor format: the field is already Unix microseconds.
    return int(value), None


def _apply_time(
    flat: dict[str, Any], spec: TimeSpec, row: dict[str, Any], warnings: list[str],
    resolve: Callable[[str | None], str | None], file_values: dict[str, Any], env: BuildEnv, ctx: PipeContext,
    preset_values: dict[str, Any],
) -> None:
    lower_raw, lower_col = _resolve_ref(spec.lower, row, resolve, file_values, env, preset_values)
    upper_raw, upper_col = _resolve_ref(spec.upper, row, resolve, file_values, env, preset_values)
    # Parse the source's zone declaration (UTC+01:00 / UTC+01:00[DST] / local / a const).
    zone_token = None
    if spec.zone is not None:
        zone_raw, _ = _resolve_field(spec.zone, row, warnings, resolve, file_values, env, ctx, preset_values)
        zone_token = parse_zone_token(zone_raw)
    for override in spec.overrides:
        flat[override.model_field], _ = _resolve_field(override, row, warnings, resolve, file_values, env, ctx, preset_values)

    lower_us, src_off = _decode_time(lower_raw, spec, zone_token, env.local_zone, warnings)
    upper_us, _ = _decode_time(upper_raw, spec, zone_token, env.local_zone, warnings)
    flat["time_lower_raw"] = lower_raw
    flat["time_lower_source_field"] = lower_col or _ref_label(spec.lower)
    flat["time_lower_unix_utc_us"] = lower_us
    flat["time_upper_raw"] = upper_raw
    flat["time_upper_source_field"] = upper_col or _ref_label(spec.upper)
    flat["time_upper_unix_utc_us"] = upper_us

    # utc_offset_hours = the analyst's configured DISPLAY zone (DST-aware) if set; else the
    # source's own resolved offset; else null. (local = time_*_unix_utc_us + utc_offset_hours.)
    # For an interval crossing a DST boundary, the lower bound's instant fixes the offset.
    if lower_us is not None:
        if src_off == UTC_OFFSET_UNKNOWN:
            # The source zone was unknown (a naive value assumed UTC); flag it loudly and do
            # NOT overwrite with a display-zone offset, which would imply a known offset.
            flat["utc_offset_hours"] = UTC_OFFSET_UNKNOWN
        elif env.local_zone:
            flat["utc_offset_hours"] = zone_offset_hours_at(env.local_zone, lower_us)
        elif src_off is not None:
            flat["utc_offset_hours"] = src_off


def _ref_label(ref: Ref) -> str | None:
    return ref.arg if ref.kind in ("field", "header") else None


def _assertion_from_flat(flat: dict[str, Any]) -> SpatioTemporalAssertion:
    """Compose the six frozen families from the fully-assembled flat working dict.

    HOW: every canonical column is read by name from ``flat`` (which carries the whole
    ``OUTPUT_COLUMNS`` key set) into the family field of the same name, so
    ``assertion.to_flat_row()`` re-emits exactly the value the pre-model engine held.
    WHY: the resolution logic still writes scattered ``model_field`` values into one dict
    (the mutable holder); building the frozen dataclasses here — not incrementally — keeps
    the model immutable while guaranteeing byte-parity with the previous flat frame. The
    controlled-vocabulary fields receive the raw resolved strings verbatim (``to_flat_row``
    passes non-enum values through unchanged), so the projection is identical."""
    return SpatioTemporalAssertion(
        entity=Entity(
            entity=flat["entity"],
            entity_type=flat["entity_type"],
            linked_entity=flat["linked_entity"],
        ),
        temporal=Temporal(
            lower=TemporalBound(
                raw=flat["time_lower_raw"],
                source_field=flat["time_lower_source_field"],
                unix_us=flat["time_lower_unix_utc_us"],
            ),
            upper=TemporalBound(
                raw=flat["time_upper_raw"],
                source_field=flat["time_upper_source_field"],
                unix_us=flat["time_upper_unix_utc_us"],
            ),
            utc_offset_hours=flat["utc_offset_hours"],
            accuracy_us=flat["time_accuracy_us"],
            temporal_source=flat["temporal_source"],
        ),
        spatial=Spatial(
            latitude_wgs84=flat["latitude_wgs84"],
            latitude_source_field=flat["latitude_source_field"],
            longitude_wgs84=flat["longitude_wgs84"],
            longitude_source_field=flat["longitude_source_field"],
            altitude_m=flat["altitude_m"],
            position_type=flat["position_type"],
            raw_position=flat["raw_position"],
            position_source=flat["position_source"],
            horizontal_accuracy_m=flat["horizontal_accuracy_m"],
            vertical_accuracy_m=flat["vertical_accuracy_m"],
            horizontal_speed_kmh=flat["horizontal_speed_kmh"],
            vertical_speed_kmh=flat["vertical_speed_kmh"],
            horizontal_speed_accuracy_kmh=flat["horizontal_speed_accuracy_kmh"],
            vertical_speed_accuracy_kmh=flat["vertical_speed_accuracy_kmh"],
            heading_deg=flat["heading_deg"],
            heading_accuracy_deg=flat["heading_accuracy_deg"],
            beam_azimuth_deg=flat["beam_azimuth_deg"],
            beam_width_deg=flat["beam_width_deg"],
        ),
        bindings=Bindings(
            entity_position_link=flat["entity_position_link"],
            entity_time_link=flat["entity_time_link"],
            spatial_temporal_link=flat["spatial_temporal_link"],
        ),
        provenance=Provenance(
            raw_source_path=flat["raw_source_path"],
            input_file_path=flat["input_file_path"],
            input_record_id=flat["input_record_id"],
            source_record_uid=flat["source_record_uid"],
            row_uid=flat["row_uid"],
            preset_id=flat["preset_id"],
            preset_name=flat["preset_name"],
            source_label=flat["source_label"],
            source_tier=flat["source_tier"],
            deleted=flat["deleted"],
        ),
        # Untangle fills record_type / record_rank later on the flat frame; empty here.
        derived=Derived(),
    )


def _record_breadcrumb(container: str | None, position: int, record: dict[str, Any]) -> str:
    """A concise ``exc.add_note`` breadcrumb naming which source record was being assembled
    when a transform raised. Names the table/line and the record's column names (not values —
    values may be sensitive and are captured, redacted, in the crash report's frame locals)."""
    line = position + 1
    locator = f"{container}#{line}" if container else f"#{line}"
    columns = ", ".join(str(key) for key in list(record.keys())[:20])
    return f"mATLAS build_assertions: failed while assembling source record {locator} (columns: {columns})"


def build_assertions(
    records: list[dict[str, Any]],
    preset: PresetSpec,
    env: BuildEnv,
    store: RunStore,
    *,
    selector: InputSelector | None = None,
    fields: list[str] | None = None,
    container_chain: str | None = None,
    recovery_state: tuple[str, ...] | None = None,
) -> tuple[list[str], int]:
    """Assemble one extracted source into typed assertions, adding them to ``store``.

    Each source record fans out into one or more ``SpatioTemporalAssertion`` objects (one
    per assertion template x time spec); the composed families and the verbatim source row
    are added to the store as ``AssertionRow`` / ``RecordRow``. Returns ``(warnings,
    assertion_count)`` — the transform warnings and the number of assertions produced (used
    by the pipeline for its per-source log line and the ``source`` row's counts).
    ``container_chain`` is the human-readable outer..inner container path recorded on the
    ``source`` row. ``recovery_state`` is the reader's row-aligned enrichment (live / wal /
    ...), stored per record but NOT projected to the flat frame this phase.
    """
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    assertion_count = 0
    source_fields = fields if fields is not None else (list(records[0].keys()) if records else [])
    resolve = make_resolver(source_fields)
    ctx = PipeContext(lookup_tables=preset.lookup_tables, patterns=preset.patterns)
    preset_values = _preset_values(preset, selector)
    container = _source_container(selector)

    # Deterministic source id (fingerprint|raw_source_path|preset_id) so the same source read
    # from a folder or a zip yields the same SourceRow; it keys the records back to their
    # source. The source row itself is added AFTER the record loop below, once the record and
    # assertion counts are known (SQLite foreign keys are not enforced here, so the records may
    # reference it before it exists). Not projected to the flat frame beyond input_file_path /
    # preset_id / preset_name.
    source_id = str(uuid.uuid5(
        _SOURCE_NAMESPACE, f"{env.source_fingerprint}|{env.raw_source_path}|{preset.meta.id}"))
    # The entity->linked_entity attribution came from the operator when a run-level
    # linked_entity was supplied, otherwise from the preset mapping. Carried on every
    # AssertionRow; never a flat-frame column.
    attribution_origin = ATTRIBUTION_OPERATOR if env.linked_entity is not None else ATTRIBUTION_PRESET

    # A run-level entity / linked_entity may itself be a call-form reference (e.g.
    # ``field(Evidence number)``) so ONE file carrying several subjects resolves per row.
    # A plain literal parses to None here and is applied verbatim to every row (below).
    entity_expr = parse_param_expr(env.entity)
    linked_entity_expr = parse_param_expr(env.linked_entity)

    file_name = env.source_file_name or env.input_file_name
    file_values: dict[str, Any] = {
        "name": file_name,
        "stem": Path(file_name).stem if file_name else None,
        "path": env.source_file_path,
    }

    # The per-record body is a nested function so the driving loop below can wrap each call and,
    # on any unhandled error, attach a breadcrumb naming WHICH source record failed (exc.add_note
    # is stdlib >= 3.11 and appears in the formatted traceback / crash report). Closures capture
    # the resolver, context, env and the shared store/warnings/seen_ids accumulators for free, so
    # the resolution body below is unchanged from the original loop body.
    def _process_record(position: int, record: dict[str, Any]) -> None:
        nonlocal assertion_count
        # Resolve any per-row entity / linked_entity reference into a concrete literal for
        # THIS record, so both the preset's ``param(...)`` refs and the override below see
        # the same resolved value. A plain literal (expr is None) is used as-is.
        row_env = env
        if entity_expr is not None or linked_entity_expr is not None:
            row_env = replace(
                env,
                entity=_resolve_param(entity_expr, env.entity, record, resolve, file_values, env, preset_values),
                linked_entity=_resolve_param(
                    linked_entity_expr, env.linked_entity, record, resolve, file_values, env, preset_values),
            )
        base = dict.fromkeys(OUTPUT_COLUMNS)
        for field_spec in preset.common:
            base[field_spec.model_field], _ = _resolve_field(
                field_spec, record, warnings, resolve, file_values, row_env, ctx, preset_values)
        # Run-level entity/linked_entity OVERRIDE the preset value when supplied.
        if row_env.entity:
            base["entity"] = row_env.entity
        if row_env.linked_entity:
            base["linked_entity"] = row_env.linked_entity
        # Engine-set provenance: the outermost artifact matlas opened, the applied preset,
        # and the tier (a preset value overrides the run default).
        base["input_file_path"] = env.input_file_path
        base["preset_id"] = preset.meta.id
        base["preset_name"] = preset.meta.title
        if base.get("source_tier") is None:
            base["source_tier"] = preset.source_tier or env.source_tier
        # raw_source_path defaults to the inner-container logical path when unmapped.
        if base.get("raw_source_path") is None:
            base["raw_source_path"] = env.raw_source_path
        # The physical 1-based line number of this record within the extracted source. It is
        # NOT a column (input_record_id carries it by default); it is the always-unique
        # disambiguator used internally for the UIDs, even when a preset maps input_record_id
        # to a non-unique tool locator.
        source_line_number = position + 1
        # input_record_id defaults to "<table-or-sheet>#<line>" (1-based) when the preset maps none.
        if base.get("input_record_id") is None:
            base["input_record_id"] = f"{container}#{source_line_number}" if container else f"#{source_line_number}"

        source_record_uid, was_mapped = _source_record_uid(
            preset, base, record, warnings, resolve, file_values, row_env, ctx, preset_values, source_line_number)
        # Only an explicitly MAPPED source_record_uid can collide: the generated one is keyed
        # on the unique source line number. A non-unique mapped "stable id" is a preset
        # authoring error, so flag it loudly rather than silently merge evidence.
        if was_mapped:
            previous = seen_ids.get(source_record_uid)
            if previous is not None and previous != position:
                raise ValueError(
                    f"mapped source_record_uid collision {source_record_uid!r} between source rows "
                    f"{previous} and {position}; the field mapped to source_record_uid is not unique"
                )
            seen_ids[source_record_uid] = position
        base["source_record_uid"] = source_record_uid

        # Record the verbatim source row once. Its ``orig_<col>`` passthrough columns are
        # exploded by RunStore.to_flat_frame (not repeated per output row here). recovery_state
        # is row-aligned engine enrichment, kept off the flat frame.
        store.add_record(RecordRow(
            source_record_uid=source_record_uid,
            source_id=source_id,
            line_no=source_line_number,
            input_record_id=base["input_record_id"],
            recovery_state=recovery_state[position] if recovery_state is not None else None,
            raw=dict(record),
        ))

        # Each source record fans out into one or more output rows; row_uid uniquely
        # identifies each. It is a deterministic uuid5 over: the OUTPUT row's own MODEL data
        # (the assertion content — which differs between a record's fan-out rows), the source
        # LINE number (input), and the OUTPUT ordinal — scoped by the source identity
        # (fingerprint + path). Content-addressed yet unique: the line number separates
        # identical-data records, the output ordinal separates a record's fan-out rows.
        source_scope = f"{env.source_fingerprint}|{base.get('raw_source_path')}|{source_line_number}"
        output_ordinal = 0
        for template in preset.assertions:
            shared = dict(base)
            for field_spec in template.fields:
                value, source_col = _resolve_field(
                    field_spec, record, warnings, resolve, file_values, row_env, ctx, preset_values)
                shared[field_spec.model_field] = value
                # Capture lat/lon provenance automatically, mirroring temporal bounds.
                if source_col is not None and field_spec.model_field == "latitude_wgs84":
                    shared["latitude_source_field"] = source_col
                elif source_col is not None and field_spec.model_field == "longitude_wgs84":
                    shared["longitude_source_field"] = source_col
            for time_spec in template.temporal:
                flat = dict(shared)
                _apply_time(flat, time_spec, record, warnings, resolve, file_values, row_env, ctx, preset_values)
                # Compose the frozen families from the now-complete flat holder (row_uid still
                # None), then derive row_uid from the MODEL projection. The content digest
                # excludes volatile provenance + the ids + untangle ranks (row_uid itself is a
                # non-content column, so it does not feed its own hash).
                assertion = _assertion_from_flat(flat)
                flat_row = assertion.to_flat_row()
                # row_uid hashes the model projection over the content columns (same repr,
                # same order as the pre-model flat digest — golden tests are the parity
                # oracle). row_uid itself is a non-content column, so it never feeds its
                # own hash. This keeps folder==zip / cross-run stability.
                content_digest = "|".join(f"{c}={flat_row[c]!r}" for c in _ROW_UID_CONTENT_COLUMNS)
                row_uid = str(uuid.uuid5(
                    _OUTPUT_ROW_NAMESPACE, f"{source_scope}|{output_ordinal}|{content_digest}"))
                output_ordinal += 1
                # Set row_uid on the frozen assertion (replace, not mutate) and store it.
                assertion = replace(
                    assertion, provenance=replace(assertion.provenance, row_uid=row_uid))
                store.add_assertion(AssertionRow(
                    row_uid=row_uid,
                    source_record_uid=source_record_uid,
                    assertion=assertion,
                    attribution_origin=attribution_origin,
                ))
                assertion_count += 1

    for position, record in enumerate(records):
        try:
            _process_record(position, record)
        except Exception as exc:  # noqa: BLE001 - annotate WHICH record failed, then re-raise unchanged.
            # add_note leaves the original exception/type intact (no wrapping): behaviour is
            # unchanged, but the crash report gains a human-readable "which record" locator.
            exc.add_note(_record_breadcrumb(container, position, record))
            raise

    # Register the source now that its record/assertion counts are known. INSERT OR IGNORE
    # keeps it idempotent should the same source be assembled twice into one store.
    store.add_source(SourceRow(
        source_id=source_id,
        input_file_path=env.input_file_path,
        container_chain=container_chain,
        raw_source_path=env.raw_source_path,
        source_fingerprint=env.source_fingerprint,
        format=selector.format if selector is not None else None,
        subunit=container,
        preset_id=preset.meta.id,
        preset_name=preset.meta.title,
        source_tier=preset.source_tier or env.source_tier,
        record_count=len(records),
        assertion_count=assertion_count,
    ))

    return warnings, assertion_count
