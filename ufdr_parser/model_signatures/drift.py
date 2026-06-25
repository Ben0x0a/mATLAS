"""Compare a dump's observed shape against the baseline and report model drift.

Defines:    DriftReport (the shifts found) and check_drift (compare + record).
Used by:    cli (``--check``) and tests.
Depends on: baseline (the store), dump.DumpSummary (the observed shape, type-only import).

On the first run against an empty baseline the shape is simply established (no warnings).
On later runs the observed shape is diffed against the baseline: a new model type, a new
relation, or a new/removed field name is a *model shift* worth an examiner's attention
(a parser/firmware change may have altered the report). Every observed element is then
recorded so the baseline tracks the latest shape (and last-seen time).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ufdr_parser.model_signatures.baseline import Baseline

if TYPE_CHECKING:
    from ufdr_parser.dump import DumpSummary

log = logging.getLogger(__name__)


@dataclass
class DriftReport:
    """The model shifts found when checking a dump against a baseline."""

    baseline_established: bool = False
    new_model_types: list[tuple[int, str]] = field(default_factory=list)
    new_relations: list[tuple[str, str]] = field(default_factory=list)
    new_fields: list[tuple[int, str, str]] = field(default_factory=list)
    removed_fields: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def has_shifts(self) -> bool:
        return bool(
            self.new_model_types
            or self.new_relations
            or self.new_fields
            or self.removed_fields
        )

    def warnings(self) -> list[str]:
        """Human-readable shift lines (empty when the baseline was just established)."""
        out: list[str] = []
        for level, model_type in sorted(self.new_model_types):
            out.append(f"new model type: {model_type} (level {level})")
        for child, parent in sorted(self.new_relations):
            out.append(f"new relation: {child} -> {parent}")
        for level, model_type, name in sorted(self.new_fields):
            out.append(f"new field: {model_type}.{name} (level {level})")
        for level, model_type, name in sorted(self.removed_fields):
            out.append(f"removed field: {model_type}.{name} (level {level})")
        return out


def _observed(summary: "DumpSummary") -> tuple[
    set[tuple[int, str]], set[tuple[str, str]], set[tuple[int, str, str]]
]:
    types = {
        (level, model_type)
        for level, model_types in summary.types_by_level.items()
        for model_type in model_types
    }
    relations = set(summary.relations)
    fields = {
        (level, model_type, name)
        for (level, model_type), names in summary.fields_by_type.items()
        for name in names
    }
    return types, relations, fields


def check_drift(summary: "DumpSummary", baseline_path: Path) -> DriftReport:
    """Diff ``summary`` against the baseline at ``baseline_path``, then record it.

    Returns a DriftReport. The first run against an empty baseline establishes it
    (``baseline_established=True``) and reports no shifts.
    """
    baseline = Baseline(baseline_path)
    try:
        obs_types, obs_relations, obs_fields = _observed(summary)
        report = DriftReport(baseline_established=baseline.is_empty())

        if not report.baseline_established:
            known_types = baseline.known_model_types()
            known_relations = baseline.known_relations()
            known_fields = baseline.known_fields()
            report.new_model_types = sorted(obs_types - known_types)
            report.new_relations = sorted(obs_relations - known_relations)
            report.new_fields = sorted(obs_fields - known_fields)
            # A removed field only counts where the owning (level, type) appears this run,
            # so an absent type (simply not in this report) is not flagged as field loss.
            observed_owners = {(level, mt) for (level, mt) in obs_types}
            report.removed_fields = sorted(
                (level, mt, name)
                for (level, mt, name) in known_fields - obs_fields
                if (level, mt) in observed_owners
            )

        _record(baseline, obs_types, obs_relations, obs_fields)
        baseline.commit()
        return report
    finally:
        baseline.close()


def _record(
    baseline: Baseline,
    types: set[tuple[int, str]],
    relations: set[tuple[str, str]],
    fields: set[tuple[int, str, str]],
) -> None:
    for level, model_type in types:
        baseline.record_model_type(level, model_type)
    for child, parent in relations:
        baseline.record_relation(child, parent)
    for level, model_type, name in fields:
        baseline.record_field(level, model_type, name)
