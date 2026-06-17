"""Assembly engine: build flat assertion rows from extracted data + a preset spec.

Defines:    BuildEnv (pipeline-derived provenance defaults), to_records (NaN->None
            normalisation), and build_rows (the fan-out + pipe + raw-capture engine).
Used by:    the v2 pipeline.
Depends on: model.families (OUTPUT_COLUMNS), presets.spec, transforms.registry + builtins.

One source row x each assertion template x each temporal spec -> one flat row. The
temporal bound's raw value and source field are captured automatically before its
pipe runs, so the raw<->normalised pairing cannot be forgotten by a preset author.
``source_row_id`` is shared by every assertion of a row and guarded for uniqueness.
"""
from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import model_atlas.transforms.builtin  # noqa: F401 - registers builtins
from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.presets.spec import FieldSpec, PresetSpec, TemporalSpec
from model_atlas.transforms.registry import apply_pipe

# Fixed namespace so deterministic source_row_ids are stable across runs/machines.
_SOURCE_ROW_NAMESPACE = uuid.UUID("6f9b1d2e-7c3a-5e41-9a2b-0c8d4e1f2a3b")

_PROVENANCE_DEFAULT_FIELDS = ("acquisition_path", "source_file_path", "input_file", "source_tier")


@dataclass(frozen=True)
class BuildEnv:
    """Pipeline/operator defaults. Values fill matching model fields only when
    the preset did not set them."""

    acquisition_path: str | None = None
    source_file_path: str | None = None
    input_file: str | None = None
    source_tier: str | None = None
    entity: str | None = None
    linked_entity: str | None = None
    source_file_name: str | None = None  # used by `from_file` field mappings


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        # pandas reads empty cells as NaN; treat any NaN-like scalar as missing so a
        # pipe sees None rather than a float nan slipping through untouched.
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
    """Resolve a column reference to an actual column name.

    A glob pattern (``* ? [``) is matched against the source columns; a unique match
    wins, several is a hard authoring error, none yields None. A plain name resolves
    to itself only if present (else None), so the timezone-in-header problem is fixed
    by writing ``from: "... - * (dd.MM.yyyy)"``.
    """
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


def _resolve_field(
    spec: FieldSpec,
    row: dict[str, Any],
    warnings: list[str],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
) -> Any:
    if spec.is_constant:
        value: Any = spec.value
    elif spec.from_name_pattern is not None:
        # The value IS the matched column's name (e.g. to extract the timezone from it).
        value = resolve(spec.from_name_pattern)
    elif spec.file_token is not None:
        # The value comes from the source FILE identity, not a column — lets a preset
        # combine filename-derived and column-derived fields on the same row.
        value = file_values.get(spec.file_token)
    else:
        actual = resolve(spec.column)
        value = row.get(actual) if actual is not None else None
    result, step_warnings = apply_pipe(value, list(spec.pipe))
    warnings.extend(step_warnings)
    return result


def _source_row_id(preset: PresetSpec, base: dict[str, Any], row: dict[str, Any], warnings: list[str], resolve: Callable[[str | None], str | None], file_values: dict[str, Any]) -> str:
    if preset.source_row_id is not None:
        resolved = _resolve_field(preset.source_row_id, row, warnings, resolve, file_values)
        return str(resolved)
    # Deterministic over the source identity, so re-processing yields identical ids.
    identity = f"{base.get('acquisition_path')}|{base.get('source_file_path')}|{base.get('record_locator')}"
    return str(uuid.uuid5(_SOURCE_ROW_NAMESPACE, identity))


def _apply_temporal(flat: dict[str, Any], spec: TemporalSpec, row: dict[str, Any], warnings: list[str], resolve: Callable[[str | None], str | None], file_values: dict[str, Any]) -> None:
    lower_col = resolve(spec.lower_column)
    upper_col = resolve(spec.upper_column)
    lower_raw = row.get(lower_col) if lower_col is not None else None
    upper_raw = row.get(upper_col) if upper_col is not None else None
    lower_ns, w1 = apply_pipe(lower_raw, list(spec.pipe))
    upper_ns, w2 = apply_pipe(upper_raw, list(spec.pipe))
    warnings.extend(w1 + w2)
    flat["time_lower_raw"] = lower_raw
    # Record the resolved column name, not the pattern, for auditability.
    flat["time_lower_source_field"] = lower_col or spec.lower_column
    flat["time_lower_unix_ns"] = lower_ns
    flat["time_upper_raw"] = upper_raw
    flat["time_upper_source_field"] = upper_col or spec.upper_column
    flat["time_upper_unix_ns"] = upper_ns
    for override in spec.overrides:
        flat[override.model_field] = _resolve_field(override, row, warnings, resolve, file_values)


def build_rows(
    records: list[dict[str, Any]],
    preset: PresetSpec,
    env: BuildEnv,
    *,
    columns: list[str] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the flat 40-column rows for one extracted source."""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    resolve = make_resolver(columns if columns is not None else (list(records[0].keys()) if records else []))

    # Source-file identity tokens for `from_file` mappings. ``name`` is the source's
    # display name (e.g. "Cache.sqlite" or "export.xlsx::sheet=Locations"); ``path`` is
    # the original/internal source path; ``stem`` drops the suffix from ``name``.
    file_name = env.source_file_name or env.input_file
    file_values: dict[str, Any] = {
        "name": file_name,
        "stem": Path(file_name).stem if file_name else None,
        "path": env.source_file_path,
    }

    for position, record in enumerate(records):
        base = dict.fromkeys(OUTPUT_COLUMNS)
        for field_spec in preset.common:
            base[field_spec.model_field] = _resolve_field(field_spec, record, warnings, resolve, file_values)
        # Run-level entity/linked_entity fill only what the preset left unset, so a
        # preset that already maps these wins over the operator-supplied defaults.
        if base.get("entity") is None and env.entity:
            base["entity"] = env.entity
        if base.get("linked_entity") is None and env.linked_entity:
            base["linked_entity"] = env.linked_entity
        # Pipeline-known provenance fills only what the preset left unset.
        for field_name, env_value in zip(
            _PROVENANCE_DEFAULT_FIELDS,
            (env.acquisition_path, env.source_file_path, env.input_file, preset.source_tier or env.source_tier),
        ):
            if base.get(field_name) is None:
                base[field_name] = env_value

        row_id = _source_row_id(preset, base, record, warnings, resolve, file_values)
        previous = seen_ids.get(row_id)
        if previous is not None and previous != position:
            # Distinct source rows must not share an id; if they do, record_locator is
            # too coarse to separate them — a hard integrity failure, not a warning.
            raise ValueError(
                f"source_row_id collision {row_id!r} between source rows {previous} and {position}; "
                f"record_locator {base.get('record_locator')!r} is not unique"
            )
        seen_ids[row_id] = position
        base["source_row_id"] = row_id

        for template in preset.assertions:
            shared = dict(base)
            for field_spec in template.fields:
                shared[field_spec.model_field] = _resolve_field(field_spec, record, warnings, resolve, file_values)
            for temporal_spec in template.temporal:
                flat = dict(shared)
                _apply_temporal(flat, temporal_spec, record, warnings, resolve, file_values)
                rows.append(flat)

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS))
    return frame, warnings
