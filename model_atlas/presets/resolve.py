"""Pure resolution of preset mapping values against one source row.

Defines:    is_missing (None/NaN test), preset_values (the ``preset(<key>)`` value
            holder), resolve_ref (one typed reference call), resolve_template
            (``format(...)`` templates — decision 42), resolve_param (run-level
            entity/linked_entity params), resolve_cond (decision-33 conditional
            chains, with cond_match), coerce_scalar and resolve_field (the full
            resolution tail: ref/conditional -> extract -> pipe -> unit -> cast).
Used by:    ingest.assemble (the L1 fan-out engine resolves every mapped field
            through these functions).
Depends on: presets.expr (Ref/Cond/Template/NA sentinels), presets.spec
            (FieldSpec/InputSelector/PresetSpec), presets.pipes (run_pipe +
            PipeContext), core.vocabularies (column_cast / unit_factor), pandas.

T60 fold: these are the pure resolver functions of the old
``transforms/assemble.py`` (formerly ``_is_missing`` /
``_resolve_ref`` / ... — private to that module), moved here under public names.
WHY the presets package: they are the EVALUATION half of the preset mapping
language — ``presets.expr`` parses a value into refs/conditionals/templates and
this module resolves them against a row — so parser and evaluator live together,
and the ingest engine depends one-way on presets. The T22 conditional
(decision 33) / template (decision 42) semantics — first-match-wins,
``NA_SENTINEL`` on no-match, empty-rendered missing placeholders — are carried
verbatim; every function is a pure function of its arguments.

``env`` is duck-typed: only ``.entity`` / ``.linked_entity`` are read (for
``param(...)`` refs), so any narrow holder with those two attributes satisfies it.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import pandas as pd

from model_atlas.core.vocabularies import column_cast, unit_factor
from model_atlas.presets.expr import NA_LITERAL, NA_SENTINEL, Cond, Ref, Template
from model_atlas.presets.pipes import PipeContext, run_pipe
from model_atlas.presets.spec import FieldSpec, InputSelector, PresetSpec


def is_missing(value: Any) -> bool:
    """True for a missing cell (None or NaN). The decision-33 sentinel is NOT missing —
    it is a real produced value and flows onward."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def preset_values(preset: PresetSpec, selector: "InputSelector | None") -> dict[str, Any]:
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


def coerce_scalar(value: Any, target: str) -> Any:
    if target == "int":
        return int(value)
    if target == "float":
        return float(value)
    if target == "str":
        return str(value)
    if target == "bool":
        return bool(value)
    raise ValueError(f"unsupported cast target {target!r}")


def resolve_ref(
    ref: Ref,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: Any,
    preset_values_map: dict[str, Any],
) -> tuple[Any, str | None]:
    """Return (value, source_field) for a reference. source_field is set only when
    the value originates from a named source field (field/header), for provenance."""
    if ref.kind == "const":
        return ref.arg, None
    if ref.kind == "format":
        return resolve_template(ref.arg, row, resolve, file_values, env, preset_values_map), None
    if ref.kind == "param":
        return (env.entity if ref.arg == "entity" else env.linked_entity), None
    if ref.kind == "filename":
        return file_values.get(ref.arg), None
    if ref.kind == "preset":
        return preset_values_map.get(ref.arg), None
    if ref.kind == "header":
        col = resolve(ref.arg)
        return col, col  # the value IS the matched field's header text
    col = resolve(ref.arg)  # field
    return (row.get(col) if col is not None else None), col


def resolve_template(
    template: Template,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: Any,
    preset_values_map: dict[str, Any],
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
            value, _ = resolve_ref(segment, row, resolve, file_values, env, preset_values_map)
            if is_missing(value):
                parts.append("")   # a missing placeholder collapses to empty; connectors stay
            else:
                any_present = True
                parts.append(str(value))
        else:
            parts.append(segment)
    if has_placeholder and not any_present:
        return NA_SENTINEL
    return "".join(parts)


def resolve_param(
    ref: Ref | None,
    literal: str | None,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: Any,
    preset_values_map: dict[str, Any],
) -> str | None:
    """Resolve a run-level param for one row. ``ref`` (from ``parse_param_expr``) resolves
    per row when set; otherwise the plain ``literal`` is used verbatim. Non-null values are
    stringified so the entity / linked_entity columns stay textual."""
    if ref is None:
        return literal
    value, _ = resolve_ref(ref, row, resolve, file_values, env, preset_values_map)
    return None if value is None else str(value)


def resolve_cond(
    cond: Cond,
    row: dict[str, Any],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: Any,
    preset_values_map: dict[str, Any],
) -> tuple[Any, str | None]:
    """Evaluate a conditional mapping value (decision 33). Branches are tested top-to-bottom
    and the FIRST match wins; its ``then`` ref is resolved and returned. With no match and no
    ``else`` the result is ``NA_SENTINEL`` — a distinct value, NOT None (None means the field
    was never mapped; NA_SENTINEL means the conditional ran and nothing matched)."""
    for branch in cond.branches:
        test_value, _ = resolve_ref(branch.test, row, resolve, file_values, env, preset_values_map)
        comparand_value, _ = resolve_ref(branch.comparand, row, resolve, file_values, env, preset_values_map)
        if cond_match(test_value, branch.op, branch.comparand, comparand_value):
            return resolve_ref(branch.then, row, resolve, file_values, env, preset_values_map)
    if cond.else_ is not None:
        return resolve_ref(cond.else_, row, resolve, file_values, env, preset_values_map)
    return NA_SENTINEL, None


def cond_match(test_value: Any, op: str, comparand_ref: Ref, comparand_value: Any) -> bool:
    """NA-aware comparison for one conditional branch. A ``const(NA)`` comparand is the
    explicit missing-value test (``field(X) == "NA"`` means "X is absent"); otherwise values
    are compared for equality, tolerating source type drift (a CSV "1" vs a numeric 1) by
    falling back to a string comparison. Only ``==``/``!=`` are supported."""
    # WHY the const(NA) special case: "NA" is not a value that lives in the source; the owner
    # made it the spelling of the missing test, so it must consult is_missing, not compare
    # the literal string "NA" against the cell.
    if comparand_ref.kind == "const" and comparand_ref.arg == NA_LITERAL:
        missing = is_missing(test_value)
        return missing if op == "==" else not missing
    if is_missing(test_value) or is_missing(comparand_value):
        equal = is_missing(test_value) and is_missing(comparand_value)
    else:
        # Direct equality first (int == int); fall back to textual equality so a source
        # value typed differently from the literal (CSV "1" vs const 1) still matches.
        equal = (test_value == comparand_value) or (str(test_value) == str(comparand_value))
    return equal if op == "==" else not equal


def resolve_field(
    spec: FieldSpec,
    row: dict[str, Any],
    warnings: list[str],
    resolve: Callable[[str | None], str | None],
    file_values: dict[str, Any],
    env: Any,
    ctx: PipeContext,
    preset_values_map: dict[str, Any],
) -> tuple[Any, str | None]:
    # A conditional (decision 33) resolves to its first-matching branch / else / NA_SENTINEL;
    # a plain ref (including a format(...) template, decision 42) resolves directly. Either may
    # yield NA_SENTINEL ("produced nothing" — a conditional with no match, or a template whose
    # every placeholder was missing); the sentinel short-circuits the transform tail below,
    # being a terminal marker, not a value to extract/pipe/unit/cast. A real value (a matched
    # branch, or a joined template) flows through the SAME tail as any ref, so type/unit/
    # extract/pipe still apply to it.
    if spec.condition is not None:
        value, source_col = resolve_cond(spec.condition, row, resolve, file_values, env, preset_values_map)
    else:
        value, source_col = resolve_ref(spec.ref, row, resolve, file_values, env, preset_values_map)
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
            value = coerce_scalar(value, target)
    return value, source_col
