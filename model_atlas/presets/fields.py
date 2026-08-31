"""Preset field primitives shared across matching, assembly and reporting.

Defines:    field_refs (field/header names a preset references), required_fields (the
            hard-dependency subset), make_resolver (glob-aware field resolver), and
            structural_score (how well a set of source columns fits a preset).
Used by:    presets.matcher, transforms.assemble, reporting, pipeline.
Depends on: presets.spec (FieldSpec/TimeSpec/PresetSpec) + the stdlib ``fnmatch`` only.

WHY this module exists: these four primitives were previously split across ``reporting``,
``transforms.assemble`` and ``presets.matcher`` and reached through private cross-module and
lazy in-function imports "to avoid a module cycle". Hoisting them into one leaf module that
depends only on ``presets.spec`` makes the dependency arrows point one way (spec <- fields <-
everything) and removes every such lazy import.
"""
from __future__ import annotations

import fnmatch
from typing import Callable

from model_atlas.presets.spec import FieldSpec, PresetSpec


def field_refs(preset: PresetSpec, *, include_optional: bool = True) -> list[str]:
    """Field/header names a preset references in its mappings.

    ``include_optional=False`` drops fields flagged ``optional: true`` — used to compute the
    REQUIRED fields (a preset's hard dependencies). The time bounds are always required (an
    observation without its instant cannot be built), so they are included regardless.
    """
    refs: list[str] = []

    def add(spec: FieldSpec | None) -> None:
        if spec is None or (not include_optional and spec.optional):
            return
        if spec.field is not None:
            refs.append(spec.field)
        if spec.from_name_pattern is not None:
            refs.append(spec.from_name_pattern)

    if preset.source_record_uid is not None:
        add(preset.source_record_uid)
    for spec in preset.common:
        add(spec)
    for template in preset.observations:
        for spec in template.fields:
            add(spec)
        for temporal in template.temporal:
            refs.extend([temporal.lower_field, temporal.upper_field])  # time is required
            add(temporal.zone)
            for override in temporal.overrides:
                add(override)
    return refs


def required_fields(preset: PresetSpec) -> set[str]:
    """The fields a preset hard-depends on (mapped, non-optional). A source missing any of
    these cannot be faithfully mapped by this preset, so it does not match."""
    return {ref for ref in field_refs(preset, include_optional=False) if ref is not None}


def make_resolver(fields: list[str]) -> Callable[[str | None], str | None]:
    """Resolve a field reference (exact name or glob) to an actual source field.

    A glob (``* ? [``) must match exactly one field; several is a hard error, none
    yields None. A plain name resolves to itself only if present."""
    available = list(fields)
    cache: dict[str, str | None] = {}

    def resolve(pattern: str | None) -> str | None:
        if pattern is None:
            return None
        if pattern in cache:
            return cache[pattern]
        if any(ch in pattern for ch in "*?["):
            matches = fnmatch.filter(available, pattern)
            if len(matches) > 1:
                raise ValueError(f"field pattern {pattern!r} matched multiple fields: {matches}")
            actual = matches[0] if matches else None
        else:
            actual = pattern if pattern in available else None
        cache[pattern] = actual
        return actual

    return resolve


def structural_score(preset: PresetSpec, columns: set[str]) -> int:
    """+1 per declared field present, -2 per absent. Uses ``expected_fields`` when set,
    else the fields the mapping references."""
    signature = list(preset.expected_fields) or field_refs(preset)
    resolve = make_resolver(sorted(columns))
    present = absent = 0
    for ref in signature:
        if resolve(ref) is not None:
            present += 1
        else:
            absent += 1
    return present - 2 * absent
