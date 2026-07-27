"""Preset linter: structured errors, warnings, and best-practice advice.

Defines:    Severity, LintFinding, lint_spec / lint_file / lint_paths — a public API
            that checks one or more v3 presets and returns findings instead of raising.
Used by:    the utils/lint_presets.py CLI, and any caller wanting to validate presets
            programmatically.
Depends on: presets.spec (parser) + presets.expr (Ref/Cond), reporting (referenced-field
            derivation), pyyaml.

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

from model_atlas.presets.expr import Ref, flatten_ref
from model_atlas.presets.spec import FieldSpec, PresetSpec, preset_spec_from_yaml

ERROR = "error"
WARNING = "warning"
ADVICE = "advice"

# Column names that are row counters, not stable UIDs (used to flag `row_uid`).
_ROWID_NAMES = {"z_pk", "rowid", "oid", "_id", "id", "pk"}

# A path segment that is a literal UUID — i.e. a hardcoded device/app id that the {uuid}
# token should generalise.
_UUID_SEGMENT = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


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
        _check_expected_fields,
        _check_mapped_fields_declared,
        _check_assertions,
        _check_unfilled_placeholders,
        _check_literal_uuid_in_path,
        _check_multi_role,
    ):
        findings.extend(check(spec, label))
    return findings


def _field_spec_refs(spec: FieldSpec) -> tuple[Ref, ...]:
    """The atomic refs a FieldSpec reads: its single ``ref``, or every ref of its
    ``condition``, each expanded through ``flatten_ref`` so a ``format(...)``
    template's placeholder fields are surfaced too. Lets the field-inventory
    checks below see every source field a conditional/template reads — a conditional or
    template referencing an undeclared field lint-WARNS, never crashes."""
    if spec.condition is not None:
        base: tuple[Ref, ...] = spec.condition.refs()
    elif spec.ref is not None:
        base = (spec.ref,)
    else:
        base = ()
    out: list[Ref] = []
    for ref in base:
        out.extend(flatten_ref(ref))
    return tuple(out)


def _plain_fields(spec: PresetSpec) -> set[str]:
    """Plain (non-glob) source fields the mapping reads, for the declared-vs-mapped check."""
    names: set[str] = set()

    def add(ref: Ref) -> None:
        arg = str(ref.arg)
        # Skip TODO placeholders — they are flagged by _check_unfilled_placeholders with a
        # clearer message, not as a phantom "undeclared field".
        if ref.kind == "field" and "TODO" not in arg and not any(ch in arg for ch in "*?["):
            names.add(arg)

    def add_spec(field: FieldSpec) -> None:
        for ref in _field_spec_refs(field):
            add(ref)

    if spec.source_record_uid is not None:
        add_spec(spec.source_record_uid)
    for field in spec.common:
        add_spec(field)
    for tmpl in spec.assertions:
        for field in tmpl.fields:
            add_spec(field)
        for time in tmpl.temporal:
            add(time.lower)
            add(time.upper)
            if time.zone is not None:
                add_spec(time.zone)
            for override in time.overrides:
                add_spec(override)
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
            "no raw_source_path mapped; map it explicitly (e.g. preset(path) for a "
            "device DB, or field(Source) for a tool export) so a row records where it came from",
            label, "common")]
    return []


def _check_record_uid_not_rowid(spec: PresetSpec, label: str) -> list[LintFinding]:
    uid = spec.source_record_uid
    # A conditional source_record_uid (ref is None) is not a single field ref, so the
    # rowid-lookalike heuristic does not apply — skip it rather than crash.
    if uid is None or uid.ref is None or uid.ref.kind != "field":
        return []
    name = str(uid.ref.arg)
    if name.casefold() in _ROWID_NAMES or name.casefold().endswith("_pk"):
        return [LintFinding(
            WARNING, "rowid-as-uid",
            f"source_record_uid maps {name!r}, which looks like a row counter, not a stable UID; "
            f"omit source_record_uid to generate a deterministic UID instead", label, "source_record_uid")]
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


def _check_expected_fields(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not spec.expected_fields:
        return [LintFinding(
            ADVICE, "no-expected-fields",
            "declare the source fields you have in expected_fields before mapping; "
            "it drives the drift report and the differential", label, "expected_fields")]
    return []


def _check_mapped_fields_declared(spec: PresetSpec, label: str) -> list[LintFinding]:
    if not spec.expected_fields:
        return []  # nothing to check against
    patterns = list(spec.expected_fields)
    out: list[LintFinding] = []
    for name in sorted(_plain_fields(spec)):
        # An expected entry may be an exact name or a glob; either may cover the field.
        if not any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            out.append(LintFinding(
                WARNING, "mapped-not-declared",
                f"mapping reads field {name!r}, which is not in expected_fields — likely a typo "
                f"or a missing declaration; an unknown field resolves to None silently, so fix "
                f"the name or add it to expected_fields", label))
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
                "only one of latitude_wgs84/longitude_wgs84 is mapped — a point needs both; map "
                "the missing one or drop the partial coordinate", label, f"{where}.position"))
        # An assertion's three links are its semantics; a missing one is incomplete.
        _link_example = {
            "entity_position": "at", "entity_time": "observed_at", "spatial_temporal": "instant",
        }
        for link in ("entity_position_link", "entity_time_link", "spatial_temporal_link"):
            if link not in fields:
                edge = link.replace("_link", "")
                out.append(LintFinding(
                    WARNING, "missing-link",
                    f"assertion declares no {edge} link — that edge's meaning is undefined; add it "
                    f"under links (e.g. {edge}: {_link_example[edge]}) so the row is interpretable",
                    label, f"{where}.links"))
        # A naive strptime parsed as UTC is a silent trap for non-UTC exports.
        for time in tmpl.temporal:
            if time.format and time.zone is None and "%z" not in time.format:
                out.append(LintFinding(
                    ADVICE, "naive-timezone",
                    "time uses format without a zone or %z; values are parsed as UTC — "
                    "declare zone if the source is not UTC", label, f"{where}.time"))
    return out


def _author_values(spec: PresetSpec) -> list[tuple[str, str]]:
    """Every author-supplied string (with a location label) that could hold a placeholder:
    selector values + all reference args + timestamp formats."""
    out: list[tuple[str, str]] = []

    def addref(ref: Ref, where: str) -> None:
        out.append((str(ref.arg), where))

    def addspec(field: FieldSpec, where: str) -> None:
        # A conditional exposes every ref it mentions, so a TODO placeholder
        # inside any branch is still surfaced by the placeholder check.
        for ref in _field_spec_refs(field):
            addref(ref, where)

    for selector in spec.input_selectors:
        for value, key in (
            (selector.name, "name"), (selector.path, "path"), (selector.table, "table"),
            (selector.sql, "sql"), (selector.sheet, "sheet"),
        ):
            if value:
                out.append((str(value), f"input_selector.{key}"))
    if spec.source_record_uid is not None:
        addspec(spec.source_record_uid, "source_record_uid")
    for field in spec.common:
        addspec(field, f"common.{field.model_field}")
    for index, tmpl in enumerate(spec.assertions):
        for field in tmpl.fields:
            addspec(field, f"assertions[{index}].{field.model_field}")
        for time in tmpl.temporal:
            addref(time.lower, f"assertions[{index}].time")
            addref(time.upper, f"assertions[{index}].time")
            if time.format:
                out.append((time.format, f"assertions[{index}].time.format"))
            if time.zone is not None:
                addspec(time.zone, f"assertions[{index}].time.zone")
            for override in time.overrides:
                addspec(override, f"assertions[{index}].{override.model_field}")
    return out


def _check_unfilled_placeholders(spec: PresetSpec, label: str) -> list[LintFinding]:
    """A generated starter still carrying a TODO placeholder must not be run as-is."""
    out: list[LintFinding] = []
    seen: set[tuple[str, str]] = set()
    for value, where in _author_values(spec):
        # Dedup per (value, location) so every distinct field carrying a TODO is surfaced
        # — identical placeholders in different fields are each the analyst's to fill.
        if "TODO" in value and (value, where) not in seen:
            seen.add((value, where))
            out.append(LintFinding(
                WARNING, "unfilled-placeholder",
                f"{value!r} is still a generator TODO placeholder — replace it with the real "
                f"field/value before use (the parser leaves every mapping to the analyst)",
                label, where))
    return out


def _check_literal_uuid_in_path(spec: PresetSpec, label: str) -> list[LintFinding]:
    """A hardcoded UUID in a selector path only matches the one acquisition it was copied
    from — the {uuid} token generalises it across devices/apps."""
    out: list[LintFinding] = []
    for selector in spec.input_selectors:
        if not selector.path:
            continue
        for segment in selector.path.replace("\\", "/").split("/"):
            if _UUID_SEGMENT.match(segment):
                out.append(LintFinding(
                    WARNING, "literal-uuid-in-path",
                    f"selector path hardcodes a UUID segment {segment!r}; it will only match "
                    f"this one acquisition — use the {{uuid}} token instead",
                    label, "input_selector.path"))
                break
    return out


def _check_multi_role(spec: PresetSpec, label: str) -> list[LintFinding]:
    """Distinct roles mean a multi-source (AND) join, which needs the python extract
    engine — not yet implemented, so flag it rather than let it fail only at run time."""
    if len(spec.roles) > 1:
        return [LintFinding(
            WARNING, "multi-role-unsupported",
            f"preset declares multiple roles {list(spec.roles)} (multi-source AND); this needs "
            f"a python extract engine, which is not yet implemented", label, "input_selector")]
    return []
