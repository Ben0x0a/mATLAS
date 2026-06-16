"""Sidecar reports for the pipeline: traceability and warnings + PEM frontier.

Defines:    the P/E/M frontier report, a human-readable traceability record, a
            PROV-JSON traceability record, and the warnings report.
Used by:    pipeline.
Depends on: presets.spec, transforms.assemble (column resolver).

The frontier follows the P/E/M view: Present source columns, Expected (declared
inventory), Mapped (consumed). The gaps are the research backlog and drift signals.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from model_atlas.presets.spec import FieldSpec, PresetSpec
from model_atlas.transforms.assemble import make_resolver

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO)


def _column_refs(preset: PresetSpec) -> list[str]:
    refs: list[str] = []

    def add(spec: FieldSpec) -> None:
        if spec.column is not None:
            refs.append(spec.column)
        if spec.from_name_pattern is not None:
            refs.append(spec.from_name_pattern)

    if preset.source_row_id is not None:
        add(preset.source_row_id)
    for spec in preset.common:
        add(spec)
    for template in preset.assertions:
        for spec in template.fields:
            add(spec)
        for temporal in template.temporal:
            refs.extend([temporal.lower_column, temporal.upper_column])
            for override in temporal.overrides:
                add(override)
    return refs


def frontier_report(preset: PresetSpec, present_columns: list[str]) -> dict[str, Any]:
    """The P/E/M view for one matched source."""
    resolve = make_resolver(list(present_columns))
    present = set(present_columns)
    expected = set(preset.expected_columns)
    mapped = {resolved for ref in _column_refs(preset) if (resolved := resolve(ref)) is not None}
    return {
        "present_count": len(present),
        "expected_count": len(expected),
        "mapped_count": len(mapped),
        # Inventoried, present, but not yet mapped — the research backlog.
        "frontier_known": sorted((present & expected) - mapped),
        # Present and unexpected and unmapped — a model shift / new column to investigate.
        "frontier_new": sorted(present - expected - mapped),
        # Expected but absent — schema drift.
        "drift_missing": sorted(expected - present),
        # Mapped but absent — a preset authoring error.
        "mapped_absent": sorted(mapped - present),
    }


def build_traceability_readable(
    *, started_at: str, input_path: Path, presets_path: Path, output_csv: Path | None,
    row_counts: dict[str, int], sources: list[dict[str, Any]], warning_count: int,
) -> dict[str, Any]:
    """A run record meant to be skimmed by a human."""
    return {
        "tool": {"name": "matlas", "pipeline": "v1", "app_tag": "matlas"},
        "run": {
            "started_at": started_at,
            "finished_at": now_iso(),
            "input": str(input_path),
            "presets": str(presets_path),
            "output_csv": str(output_csv) if output_csv else None,
        },
        "row_counts": row_counts,
        "warnings": warning_count,
        "sources": sources,
    }


def build_traceability_prov(
    *, started_at: str, output_csv: Path | None, sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """A W3C PROV-JSON record of the same run (harder to skim, standard to consume)."""
    entities: dict[str, Any] = {}
    used: dict[str, Any] = {}
    derived: dict[str, Any] = {}
    output_id = "matlas:output"
    if output_csv is not None:
        entities[output_id] = {"prov:label": str(output_csv), "matlas:type": "integration_model_csv"}
    for index, source in enumerate(sources):
        source_id = f"matlas:source/{index}"
        entities[source_id] = {
            "prov:label": source.get("source_file"),
            "matlas:source_file_path": source.get("source_file_path"),
            "matlas:preset": source.get("matched_preset"),
            "matlas:source_tier": source.get("source_tier"),
        }
        used[f"matlas:_u{index}"] = {"prov:activity": "matlas:run", "prov:entity": source_id}
        if output_csv is not None:
            derived[f"matlas:_d{index}"] = {"prov:generatedEntity": output_id, "prov:usedEntity": source_id}
    return {
        "prefix": {"matlas": "https://github.com/model-atlas/ns#", "prov": "http://www.w3.org/ns/prov#"},
        "entity": entities,
        "activity": {"matlas:run": {"prov:startTime": started_at, "prov:endTime": now_iso()}},
        "agent": {"matlas:tool": {"prov:type": "prov:SoftwareAgent", "prov:label": "matlas v1"}},
        "wasAssociatedWith": {"matlas:_a": {"prov:activity": "matlas:run", "prov:agent": "matlas:tool"}},
        "used": used,
        "wasGeneratedBy": (
            {"matlas:_g": {"prov:entity": output_id, "prov:activity": "matlas:run"}} if output_csv else {}
        ),
        "wasDerivedFrom": derived,
    }


def build_warnings_report(transform_warnings: list[str], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Transform warnings plus the per-source frontier, so unmapped columns are visible."""
    frontier = [
        {
            "source_file": source.get("source_file"),
            "preset": source.get("matched_preset"),
            "frontier": source.get("frontier"),
        }
        for source in sources
    ]
    return {
        "transform_warnings": transform_warnings,
        "transform_warning_count": len(transform_warnings),
        "frontier": frontier,
    }
