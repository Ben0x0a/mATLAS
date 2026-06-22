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
artefact), else a deterministic uuid5 over the source content fingerprint + path +
``source_record_number`` (the physical record position) — globally unique and portable; a
mapped one is guarded for uniqueness. ``source_record_number`` is the 1-based ordinal of the
record within its extracted source. ``row_uid`` is generated per OUTPUT row (deterministic
uuid5 over the ROW'S DATA + the source record number + the output ordinal, scoped by the
source identity): content-addressed, yet the record number keeps identical-data rows
distinct and the output ordinal keeps a record's fan-out rows distinct.
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
from model_atlas.presets.spec import FieldSpec, InputSelector, PresetSpec, TimeSpec
from model_atlas.transforms.builtin import epoch_to_us, parse_datetime_to_us, tz_offset_to_hours
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
        env.source_fingerprint, base.get("raw_source_path"), source_record_number,
    ))
    return str(uuid.uuid5(_SOURCE_ROW_NAMESPACE, identity)), False


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
        # The physical 1-based ordinal of this record within the extracted source — the
        # analyst's "jump to this record" handle, and the always-unique disambiguator.
        record_number = position + 1
        base["source_record_number"] = record_number
        # input_record_id defaults to "<table-or-sheet>#<number>" when the preset maps none.
        if base.get("input_record_id") is None:
            base["input_record_id"] = f"{container}#{record_number}" if container else f"#{record_number}"

        source_record_uid, was_mapped = _source_record_uid(
            preset, base, record, warnings, resolve, file_values, env, ctx, preset_values, record_number)
        # Only an explicitly MAPPED source_record_uid can collide: the generated one is
        # keyed on the unique source record number. A non-unique mapped "stable id" is a
        # preset authoring error, so flag it loudly rather than silently merge evidence.
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
        # identifies each. It is a deterministic uuid5 over the ROW'S OWN DATA + the source
        # record number + the output-row ordinal, scoped by the source identity (fingerprint
        # + path). Folding in the data makes the id content-addressed; the record number
        # guarantees uniqueness even for two rows with identical data; the output ordinal
        # separates the rows one record fans out into. Reproducible across runs and identical
        # for the same db read from a folder vs a zip.
        record_digest = "|".join(f"{key}={record[key]!r}" for key in sorted(record, key=str))
        row_uid_base = (
            f"{env.source_fingerprint}|{base.get('raw_source_path')}|{record_number}|{record_digest}"
        )
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
                flat["row_uid"] = str(uuid.uuid5(_OUTPUT_ROW_NAMESPACE, f"{row_uid_base}|{output_ordinal}"))
                output_ordinal += 1
                rows.append(flat)

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS) + list(passthrough_names.values()))
    return frame, warnings
