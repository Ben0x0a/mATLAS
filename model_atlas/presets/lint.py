"""Preset linter: structured errors, warnings, and best-practice advice.

Defines:    Severity, LintFinding, lint_spec / lint_file / lint_paths — a public API
            that checks one or more v3 presets and returns findings instead of raising.
Used by:    the utils/lint_presets.py CLI, and any caller wanting to validate presets
            programmatically.
Depends on: presets.spec (parser), reporting (referenced-column derivation), pyyaml.

Three severities:
- ERROR  : the preset does not parse/validate and will not load.
- WARNING: the preset loads but is very likely wrong (a half-coordinate, an
           assertion missing a link).
- ADVICE : a best-practice / consistency nudge (naming, tier/tool coherence,
           using a named epoch codec instead of raw arithmetic, etc.).

The linter never raises for a bad preset — that is the point; it reports.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from model_atlas.presets.spec import PresetSpec, preset_spec_from_yaml

ERROR = "error"
WARNING = "warning"
ADVICE = "advice"

# Column names that are row counters, not stable UIDs (used to flag `row_uid`).
_ROWID_NAMES = {"z_pk", "rowid", "oid", "_id", "id", "pk"}


@dataclass(frozen=True)
class LintFinding:
    severity: str            # ERROR | WARNING | ADVICE
    code: str                # stable short code, e.g. "rowid-as-uid"
    message: str
    preset: str              # source path or preset id
    location: str | None = None  # e.g. "assertions[0].links"

    def format(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity.upper():7} {self.code}: {self.message}{where}"


def lint_file(path: Path) -> list[LintFinding]:
    """Lint a single preset YAML file."""
    path = Path(path)
    label = str(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [LintFinding(ERROR, "yaml-error", f"cannot read YAML: {exc}", label)]
    try:
        spec = preset_spec_from_yaml(raw, path)
    except ValueError as exc:
        # The parser's message already carries the path; keep it as the single error.
        return [LintFinding(ERROR, "parse-error", str(exc), label)]
    return lint_spec(spec, preset_label=label)


def lint_paths(paths: Iterable[Path]) -> list[LintFinding]:
    """Lint every preset under the given files/folders (``*.yaml`` recursively)."""
    findings: list[LintFinding] = []
    for entry in paths:
        entry = Path(entry)
        files = [entry] if entry.is_file() else sorted(entry.rglob("*.yaml"))
        for file in files:
            findings.extend(lint_file(file))
    return findings


def lint_spec(spec: PresetSpec, *, preset_label: str | None = None) -> list[LintFinding]:
    """Run the best-practice checks against an already-parsed preset."""
    label = preset_label or spec.meta.id
    findings: list[LintFinding] = []
    for check in (
        _check_id_naming,
        _check_tier_tool_coherence,
        _check_os_version_present,
        _check_raw_source_path,
        _check_record_uid_not_rowid,
        _check_epoch_arithmetic_smell,
        _check_expected_columns,
        _check_mapped_columns_declared,
        _check_assertions,
    ):
        findings.extend(check(spec, label))
    return findings


def _plain_columns(spec: PresetSpec) -> set[str]:
    """Plain (non-glob) source columns the mapping reads, for the declared-vs-mapped check."""
    names: set[str] = set()

    def add(ref) -> None:
        if ref.kind == "column" and not any(ch in str(ref.arg) for ch in "*?["):
            names.add(str(ref.arg))

    if spec.record_uid is not None:
        add(spec.record_uid.ref)
    for field in spec.common:
        add(field.ref)
    for tmpl in spec.assertions:
        for field in tmpl.fields:
            add(field.ref)
        for time in tmpl.temporal:
            add(time.lower)
            add(time.upper)
            if time.zone is not None:
                add(time.zone.ref)
            for override in time.overrides:
                add(override.ref)
    return names


# --- individual checks ------------------------------------------------------

def _check_id_naming(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9_]+)+", spec.meta.id):
        return [LintFinding(
            ADVICE, "id-naming",
            f"preset id {spec.meta.id!r} should be a dotted lowercase slug "
            f"(e.g. ios.routined.cached_locations)", label, "preset.id")]
    return []


def _check_tier_tool_coherence(spec: PresetSpec, label: str) -> list[LintFinding]:
    out: list[LintFinding] = []
    tier, tool = spec.meta.tier, spec.meta.tool
    if tier == "primary" and tool:
        out.append(LintFinding(
            ADVICE, "tier-tool", "a primary (device) source usually has no tool", label, "preset.tool"))
    if tier == "secondary" and not tool:
        out.append(LintFinding(
            ADVICE, "tier-tool", "a secondary (tool export) should name its tool", label, "preset.tool"))
    return out


def _check_os_version_present(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not spec.meta.os_version:
        return [LintFinding(
            ADVICE, "no-os-version",
            "no os_version range; set one (e.g. \">=15\") so version variants tie-break cleanly",
            label, "preset.os_version")]
    return []


def _check_raw_source_path(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not any(f.model_field == "raw_source_path" for f in spec.common):
        return [LintFinding(
            WARNING, "no-raw-source-path",
            "no raw_source_path mapped; map it explicitly (e.g. preset(in_archive) for a "
            "device DB, or column(Source) for a tool export) so a row records where it came from",
            label, "common")]
    return []


def _check_record_uid_not_rowid(spec: PresetSpec, label: str) -> list[LintFinding]:
    uid = spec.record_uid
    if uid is None or uid.ref.kind != "column":
        return []
    name = str(uid.ref.arg)
    if name.casefold() in _ROWID_NAMES or name.casefold().endswith("_pk"):
        return [LintFinding(
            WARNING, "rowid-as-uid",
            f"record_uid maps {name!r}, which looks like a row counter, not a stable UID; "
            f"omit record_uid to generate a deterministic UID instead", label, "record_uid")]
    return []


def _check_epoch_arithmetic_smell(spec: PresetSpec, label: str) -> list[LintFinding]:
    out: list[LintFinding] = []
    specs = list(spec.common)
    for tmpl in spec.assertions:
        specs.extend(tmpl.fields)
    for field in specs:
        for call in field.pipe:
            blob = " ".join(str(a) for a in call.args)
            if call.name == "arithmetic" and ("978307200" in blob or "1000000000" in blob):
                out.append(LintFinding(
                    ADVICE, "raw-epoch-arithmetic",
                    f"{field.model_field}: raw epoch arithmetic detected; prefer a named "
                    f"time epoch codec (epoch: cocoa|unix_s|unix_ms|...)", label, field.model_field))
    return out


def _check_expected_columns(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not spec.expected_columns:
        return [LintFinding(
            ADVICE, "no-expected-columns",
            "declare the source columns you have in expected_columns before mapping; "
            "it drives the drift report and the differential", label, "expected_columns")]
    return []


def _check_mapped_columns_declared(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not spec.expected_columns:
        return []  # nothing to check against
    patterns = list(spec.expected_columns)
    out: list[LintFinding] = []
    for name in sorted(_plain_columns(spec)):
        # An expected entry may be an exact name or a glob; either may cover the column.
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            out.append(LintFinding(
                WARNING, "mapped-not-declared",
                f"mapping reads column {name!r}, which is not in expected_columns", label))
    return out


def _check_assertions(spec: PresetSpec, label: str) -> list[LintFinding]:
    out: list[LintFinding] = []
    for index, tmpl in enumerate(spec.assertions):
        where = f"assertions[{index}]"
        fields = {f.model_field for f in tmpl.fields}
        # Half a coordinate is almost always a mistake.
        if ("latitude_wgs84" in fields) != ("longitude_wgs84" in fields):
            out.append(LintFinding(
                WARNING, "half-coordinate",
                "only one of latitude_wgs84/longitude_wgs84 is mapped", label, f"{where}.position"))
        # An assertion's three links are its semantics; a missing one is incomplete.
        for link in ("entity_position_link", "entity_time_link", "spatial_temporal_link"):
            if link not in fields:
                out.append(LintFinding(
                    WARNING, "missing-link",
                    f"assertion has no {link.replace('_link', '')} link", label, f"{where}.links"))
        # A naive strptime parsed as UTC is a silent trap for non-UTC exports.
        for time in tmpl.temporal:
            if time.format and time.zone is None and "%z" not in time.format:
                out.append(LintFinding(
                    ADVICE, "naive-timezone",
                    "time uses format without a zone or %z; values are parsed as UTC — "
                    "declare zone if the source is not UTC", label, f"{where}.time"))
    return out
