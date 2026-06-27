"""Assembly engine: build flat assertion rows from extracted data + a v3 preset.

Defines:    BuildEnv (pipeline-derived defaults), to_records (NaN->None), make_resolver
            (glob column resolver), and build_rows (the fan-out + reference + pipe +
            raw-capture engine).
Used by:    the pipeline.
Depends on: model.families (columns/types/units), presets.spec + expr, transforms.

One source row x each assertion (each with one time spec) -> one flat row. Temporal
raw values and source fields, and the lat/lon source columns, are captured
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
import fnmatch
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import model_atlas.transforms.builtin  # noqa: F401 - registers builtins
from model_atlas.model.families import (
    OUTPUT_COLUMNS,
    UTC_OFFSET_UNKNOWN,
    column_cast,
    unit_factor,
)
from model_atlas.presets.expr import Ref
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
    # Neither epoch nor format: the column is already Unix microseconds.
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
    return ref.arg if ref.kind in ("column", "header") else None


# Prefix for verbatim source-column passthrough. NOT ``raw_`` — that is already used by
# canonical columns (``raw_position``, ``raw_source_path``).
PASSTHROUGH_PREFIX = "orig_"


def _passthrough_column_names(source_columns: list[str]) -> dict[str, str]:
    """Map each source column to its ``orig_<col>`` output name, preserving source order.

    A name that would collide with a canonical column is disambiguated with a trailing
    ``_``. The mapping is a pure function of the column name, so it is identical across
    sources (stable merges)."""
    used = set(OUTPUT_COLUMNS)
    names: dict[str, str] = {}
    for col in source_columns:
        name = f"{PASSTHROUGH_PREFIX}{col}"
        while name in used:
            name += "_"
        names[col] = name
        used.add(name)
    return names


def build_rows(
    records: list[dict[str, Any]],
    preset: PresetSpec,
    env: BuildEnv,
    *,
    selector: InputSelector | None = None,
    columns: list[str] | None = None,
    include_source_columns: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the flat canonical rows for one extracted source.

    When ``include_source_columns`` is set, every original source column is appended
    verbatim as an ``orig_<col>`` column (after the canonical schema). The values are
    taken from the source record, so each assertion a row fans out into carries the same
    passthrough columns.
    """
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    source_columns = columns if columns is not None else (list(records[0].keys()) if records else [])
    resolve = make_resolver(source_columns)
    passthrough_names = _passthrough_column_names(source_columns) if include_source_columns else {}
    ctx = PipeContext(lookup_tables=preset.lookup_tables, patterns=preset.patterns)
    preset_values = _preset_values(preset, selector)
    container = _source_container(selector)

    file_name = env.source_file_name or env.input_file_name
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
            preset, base, record, warnings, resolve, file_values, env, ctx, preset_values, source_line_number)
        # Only an explicitly MAPPED source_record_uid can collide: the generated one is keyed
        # on the unique source line number. A non-unique mapped "stable id" is a preset
        # authoring error, so flag it loudly rather than silently merge evidence.
        if was_mapped:
            previous = seen_ids.get(source_record_uid)
            if previous is not None and previous != position:
                raise ValueError(
                    f"mapped source_record_uid collision {source_record_uid!r} between source rows "
                    f"{previous} and {position}; the column mapped to source_record_uid is not unique"
                )
            seen_ids[source_record_uid] = position
        base["source_record_uid"] = source_record_uid

        # Verbatim passthrough of every source column, shared by all of this row's
        # assertions (so a pivoted source row repeats these columns on each output row).
        for col, name in passthrough_names.items():
            base[name] = record.get(col)

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
                # Content digest over the output row's MODEL fields (excludes volatile
                # provenance + the ids + untangle ranks); computed now that the row is fully
                # assembled. Combined with the source scope/line and the output ordinal.
                content_digest = "|".join(f"{c}={flat.get(c)!r}" for c in _ROW_UID_CONTENT_COLUMNS)
                flat["row_uid"] = str(uuid.uuid5(
                    _OUTPUT_ROW_NAMESPACE, f"{source_scope}|{output_ordinal}|{content_digest}"))
                output_ordinal += 1
                rows.append(flat)

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS) + list(passthrough_names.values()))
    return frame, warnings
