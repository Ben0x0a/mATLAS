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
import fnmatch
from pathlib import Path
from typing import Any

from model_atlas.presets.spec import FieldSpec, PresetSpec
from model_atlas.transforms.assemble import make_resolver

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO)


def _column_refs(preset: PresetSpec, *, include_optional: bool = True) -> list[str]:
    """Column/header names a preset references in its mappings.

    ``include_optional=False`` drops fields flagged ``optional: true`` — used to compute the
    REQUIRED columns (a preset's hard dependencies). The time bounds are always required (an
    assertion without its instant cannot be built), so they are included regardless.
    """
    refs: list[str] = []

    def add(spec: FieldSpec | None) -> None:
        if spec is None or (not include_optional and spec.optional):
            return
        if spec.column is not None:
            refs.append(spec.column)
        if spec.from_name_pattern is not None:
            refs.append(spec.from_name_pattern)

    if preset.source_record_uid is not None:
        add(preset.source_record_uid)
    for spec in preset.common:
        add(spec)
    for template in preset.assertions:
        for spec in template.fields:
            add(spec)
        for temporal in template.temporal:
            refs.extend([temporal.lower_column, temporal.upper_column])  # time is required
            add(temporal.zone)
            for override in temporal.overrides:
                add(override)
    return refs


def required_columns(preset: PresetSpec) -> set[str]:
    """The columns a preset hard-depends on (mapped, non-optional). A source missing any of
    these cannot be faithfully mapped by this preset, so it does not match."""
    return {ref for ref in _column_refs(preset, include_optional=False) if ref is not None}


def frontier_report(preset: PresetSpec, present_columns: list[str]) -> dict[str, Any]:
    """The P/E/M view for one matched source. ``expected_columns`` entries may be exact
    names or glob patterns; a pattern is matched against the present columns."""
    resolve = make_resolver(list(present_columns))
    present = set(present_columns)
    patterns = list(preset.expected_columns)
    # Present columns covered by some expected entry (exact or glob).
    expected_present = {c for c in present if any(fnmatch.fnmatch(c, p) for p in patterns)}
    refs = [ref for ref in _column_refs(preset) if ref is not None]
    mapped = {resolved for ref in refs if (resolved := resolve(ref)) is not None}
    # Referenced column names that resolve to nothing present (an exact name absent from the
    # source, or a glob matching no column) — a preset authoring error or a schema drift.
    mapped_absent = {ref for ref in refs if resolve(ref) is None}
    return {
        "present_count": len(present),
        "expected_count": len(patterns),
        "mapped_count": len(mapped),
        # Inventoried, present, but not yet mapped — the research backlog.
        "frontier_known": sorted(expected_present - mapped),
        # Present and unexpected and unmapped — a model shift / new column to investigate.
        "frontier_new": sorted(present - expected_present - mapped),
        # Expected entry that matches no present column — schema drift.
        "drift_missing": sorted(p for p in patterns if not any(fnmatch.fnmatch(c, p) for c in present)),
        # Mapped but absent — a preset authoring error (now actually detected).
        "mapped_absent": sorted(mapped_absent),
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
            "matlas:raw_source_path": source.get("raw_source_path"),
            "matlas:input_file_path": source.get("input_file_path"),
            "matlas:input_file_name": source.get("input_file_name"),
            "matlas:container_chain": source.get("container_chain"),
            "matlas:format": source.get("format"),
            "matlas:table": source.get("table"),
            "matlas:sheet": source.get("sheet"),
            "matlas:source_fingerprint": source.get("source_fingerprint"),
            "matlas:preset_id": source.get("preset_id"),
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
