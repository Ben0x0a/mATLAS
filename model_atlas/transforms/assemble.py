"""Assembly engine: build flat assertion rows from extracted data + a v3 preset.

Defines:    BuildEnv (pipeline-derived defaults), to_records (NaN->None), make_resolver
            (glob column resolver), and build_rows (the fan-out + reference + pipe +
            raw-capture engine).
Used by:    the pipeline.
Depends on: model.families (columns/types/units), presets.spec + expr, transforms.

One source row x each assertion (each with one time spec) -> one flat row. Temporal
raw values and source fields, and the lat/lon source columns, are captured
automatically so an author can never forget the value<->provenance pairing.
``record_uid`` is a UID shared by every assertion of a row: the tool's own uid when a
preset maps ``record_uid`` (so the row links back to the tool artefact), else a
deterministic uuid5 over the source content fingerprint + path + ``input_record_id`` —
globally unique and portable across machines. It is guarded for uniqueness.
"""
from __future__ import annotations

import fnmatch
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import model_atlas.transforms.builtin  # noqa: F401 - registers builtins
from model_atlas.model.families import OUTPUT_COLUMNS, column_cast, unit_factor
from model_atlas.presets.expr import Ref
from model_atlas.presets.spec import FieldSpec, PresetSpec, TimeSpec
from model_atlas.transforms.builtin import epoch_to_us, parse_datetime_to_us, tz_offset_to_hours
from model_atlas.transforms.registry import PipeContext, run_pipe

# Fixed namespace so deterministic record_uids are stable across runs/machines.
_SOURCE_ROW_NAMESPACE = uuid.UUID("6f9b1d2e-7c3a-5e41-9a2b-0c8d4e1f2a3b")


@dataclass(frozen=True)
class BuildEnv:
    """Pipeline/operator-supplied values for one source. ``input_file`` (the file matlas
    read) and ``source_fingerprint`` (the source artefact's content hash) are engine-set;
    ``source_tier`` is a default the preset may override; entity/linked_entity OVERRIDE the
    preset. ``source_file_path`` backs the ``filename(path)`` ref only."""

    input_file: str | None = None
    source_fingerprint: str | None = None
    source_file_path: str | None = None
    source_tier: str | None = None
    entity: str | None = None
    linked_entity: str | None = None
    source_file_name: str | None = None


def _source_container(preset: PresetSpec) -> str | None:
    """The table / sheet / query the rows were read from — the prefix of input_record_id."""
    match = preset.match
    return match.table or match.sheet or ("query" if match.sql else None)


def _preset_values(preset: PresetSpec) -> dict[str, Any]:
    """Values a ``preset(<key>)`` ref may read from the current preset (match + meta)."""
    m, meta = preset.match, preset.meta
    return {
        "in_archive": m.in_archive, "as_file": m.as_file, "table": m.table,
        "sheet": m.sheet, "sql": m.sql,
        "id": meta.id, "name": meta.title, "tier": meta.tier, "os": meta.os,
        "tool": meta.tool, "version": meta.version, "os_version": meta.os_version,
    }


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


def make_resolver(columns: list[str]) -> Callable[[str | None], str | None]:
    """Resolve a column reference (exact name or glob) to an actual source column.

    A glob (``* ? [``) must match exactly one column; several is a hard error, none
    yields None. A plain name resolves to itself only if present."""
    available = list(columns)
    cache: dict[str, str | None] = {}

    def resolve(pattern: str | None) -> str | None:
        if pattern is None:
            return None
        if pattern in cache:
            return cache[pattern]
        if any(ch in pattern for ch in "*?["):
            matches = fnmatch.filter(available, pattern)
            if len(matches) > 1:
                raise ValueError(f"column pattern {pattern!r} matched multiple columns: {matches}")
            actual = matches[0] if matches else None
        else:
            actual = pattern if pattern in available else None
        cache[pattern] = actual
        return actual

    return resolve


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
    """Return (value, source_column) for a reference. source_column is set only when
    the value originates from a named source column (column/header), for provenance."""
    if ref.kind == "const":
        return ref.arg, None
    if ref.kind == "param":
        return (env.entity if ref.arg == "entity" else env.linked_entity), None
    if ref.kind == "filename":
        return file_values.get(ref.arg), None
    if ref.kind == "preset":
        return preset_values.get(ref.arg), None
    if ref.kind == "header":
        col = resolve(ref.arg)
        return col, col  # the value IS the matched column's header text
    col = resolve(ref.arg)  # column
    return (row.get(col) if col is not None else None), col


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
    value, source_col = _resolve_ref(spec.ref, row, resolve, file_values, env, preset_values)
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


def _record_uid(
    preset: PresetSpec, base: dict[str, Any], row: dict[str, Any], warnings: list[str],
    resolve: Callable[[str | None], str | None], file_values: dict[str, Any], env: BuildEnv,
    ctx: PipeContext, preset_values: dict[str, Any],
) -> str:
    """The row's UID. A preset that maps ``record_uid`` supplies the tool's own value
    verbatim (so the row links back to the tool artefact); otherwise generate a
    deterministic uuid5 over the source content fingerprint + path + input_record_id, which
    is globally unique (different acquisitions differ in the fingerprint) and portable."""
    if preset.record_uid is not None:
        resolved, _ = _resolve_field(preset.record_uid, row, warnings, resolve, file_values, env, ctx, preset_values)
        return str(resolved)
    identity = "|".join(str(part) for part in (
        env.source_fingerprint, base.get("raw_source_path"), base.get("input_file"), base.get("input_record_id"),
    ))
    return str(uuid.uuid5(_SOURCE_ROW_NAMESPACE, identity))


def _decode_time(value: Any, spec: TimeSpec, zone: Any, warnings: list[str]) -> int | None:
    if value is None:
        return None
    if spec.epoch is not None:
        return epoch_to_us(value, spec.epoch)
    if spec.format is not None:
        tz: Any = 0.0
        if zone is not None:
            try:
                tz_offset_to_hours(zone)
                tz = zone
            except ValueError:
                warnings.append(f"unparseable time_zone {zone!r}; parsing as UTC")
        return parse_datetime_to_us(value, spec.format, tz)
    # Neither epoch nor format: the column is already Unix microseconds.
    return int(value)


def _apply_time(
    flat: dict[str, Any], spec: TimeSpec, row: dict[str, Any], warnings: list[str],
    resolve: Callable[[str | None], str | None], file_values: dict[str, Any], env: BuildEnv, ctx: PipeContext,
    preset_values: dict[str, Any],
) -> None:
    lower_raw, lower_col = _resolve_ref(spec.lower, row, resolve, file_values, env, preset_values)
    upper_raw, upper_col = _resolve_ref(spec.upper, row, resolve, file_values, env, preset_values)
    zone: Any = None
    if spec.zone is not None:
        zone, _ = _resolve_field(spec.zone, row, warnings, resolve, file_values, env, ctx, preset_values)
        flat["time_zone"] = zone
    for override in spec.overrides:
        flat[override.model_field], _ = _resolve_field(override, row, warnings, resolve, file_values, env, ctx, preset_values)
    flat["time_lower_raw"] = lower_raw
    flat["time_lower_source_field"] = lower_col or _ref_label(spec.lower)
    flat["time_lower_unix_us"] = _decode_time(lower_raw, spec, zone, warnings)
    flat["time_upper_raw"] = upper_raw
    flat["time_upper_source_field"] = upper_col or _ref_label(spec.upper)
    flat["time_upper_unix_us"] = _decode_time(upper_raw, spec, zone, warnings)


def _ref_label(ref: Ref) -> str | None:
    return ref.arg if ref.kind in ("column", "header") else None


def build_rows(
    records: list[dict[str, Any]],
    preset: PresetSpec,
    env: BuildEnv,
    *,
    columns: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the flat canonical rows for one extracted source."""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    resolve = make_resolver(columns if columns is not None else (list(records[0].keys()) if records else []))
    ctx = PipeContext(lookup_tables=preset.lookup_tables, patterns=preset.patterns)
    preset_values = _preset_values(preset)
    container = _source_container(preset)

    file_name = env.source_file_name or env.input_file
    file_values: dict[str, Any] = {
        "name": file_name,
        "stem": Path(file_name).stem if file_name else None,
        "path": env.source_file_path,
    }

    for position, record in enumerate(records):
        base = dict.fromkeys(OUTPUT_COLUMNS)
        for field_spec in preset.common:
            base[field_spec.model_field], _ = _resolve_field(
                field_spec, record, warnings, resolve, file_values, env, ctx, preset_values)
        # Run-level entity/linked_entity OVERRIDE the preset value when supplied.
        if env.entity:
            base["entity"] = env.entity
        if env.linked_entity:
            base["linked_entity"] = env.linked_entity
        # Engine-set provenance: the file matlas read, the applied preset, and the tier
        # (a preset value overrides the run default).
        base["input_file"] = env.input_file
        base["preset_id"] = preset.meta.id
        base["preset_name"] = preset.meta.title
        if base.get("source_tier") is None:
            base["source_tier"] = preset.source_tier or env.source_tier
        # input_record_id defaults to "<table-or-sheet>#<ordinal>" when the preset maps none.
        if base.get("input_record_id") is None:
            base["input_record_id"] = f"{container}#{position}" if container else f"#{position}"

        row_id = _record_uid(preset, base, record, warnings, resolve, file_values, env, ctx, preset_values)
        previous = seen_ids.get(row_id)
        if previous is not None and previous != position:
            raise ValueError(
                f"record_uid collision {row_id!r} between source rows {previous} and {position}; "
                f"the mapped record_uid (input_record_id {base.get('input_record_id')!r}) is not unique"
            )
        seen_ids[row_id] = position
        base["record_uid"] = row_id

        for template in preset.assertions:
            shared = dict(base)
            for field_spec in template.fields:
                value, source_col = _resolve_field(
                    field_spec, record, warnings, resolve, file_values, env, ctx, preset_values)
                shared[field_spec.model_field] = value
                # Capture lat/lon provenance automatically, mirroring temporal bounds.
                if source_col is not None and field_spec.model_field == "latitude_wgs84":
                    shared["latitude_source_field"] = source_col
                elif source_col is not None and field_spec.model_field == "longitude_wgs84":
                    shared["longitude_source_field"] = source_col
            for time_spec in template.temporal:
                flat = dict(shared)
                _apply_time(flat, time_spec, record, warnings, resolve, file_values, env, ctx, preset_values)
                rows.append(flat)

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS))
    return frame, warnings
