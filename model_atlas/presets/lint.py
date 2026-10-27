"""Preset linter: structured errors, warnings, and best-practice advice.

Defines:    Severity, LintFinding, lint_spec / lint_file / lint_paths — a public API
            that checks one or more v3 presets and returns findings instead of raising.
Used by:    the utils/lint_presets.py CLI, and any caller wanting to validate presets
            programmatically.
Depends on: presets.spec (parser + the admitted-key/assignable vocabularies) +
            presets.expr (Ref/Cond), core.vocabularies (the free-text controlled
            vocabularies), integrate.timeparse (zone tokens), pyyaml.

Two passes:
- ``check_raw_document`` runs on the YAML tree BEFORE parsing, so a preset that does not
  load is still diagnosed (the parser raises on the first problem and says nothing about
  the rest);
- ``lint_spec`` runs the best-practice checks on the parsed spec.

Findings carry an optional ``suggestion``: the YAML to write instead. A check that knows
what is wrong usually knows what is right, and the fix is what the author needs.

Three severities:
- ERROR  : the preset does not parse/validate and will not load.
- WARNING: the preset loads but is very likely wrong (a half-coordinate, an
           observation missing a link).
- ADVICE : a best-practice / consistency nudge (naming, tier/tool coherence,
           using a named epoch codec instead of raw arithmetic, etc.).

The linter never raises for a bad preset — that is the point; it reports.
"""
from __future__ import annotations

import difflib
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from model_atlas.core import vocabularies as V
from model_atlas.integrate.timeparse import parse_zone_token
from model_atlas.presets.expr import Ref, flatten_ref, is_condition
from model_atlas.presets.spec import (
    DURATION_BOUND_NAME,
    LINK_FIELDS,
    TOP_LEVEL_KEYS,
    FieldSpec,
    PresetSpec,
    is_assignable,
    preset_spec_from_yaml,
)

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
    location: str | None = None  # e.g. "observations[0].links"
    # The YAML to write instead — a correction the author can paste, not a restatement of the
    # problem. WHY a separate field and not more prose in ``message``: the message says what is
    # wrong (and is what a caller greps or groups by), the suggestion says what to do, and a
    # renderer can show, indent or suppress the two independently.
    suggestion: str | None = None

    def format(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        head = f"{self.severity.upper():7} {self.code}: {self.message}{where}"
        return head if self.suggestion is None else f"{head}\n        → fix: {self.suggestion}"


def lint_file(path: Path) -> list[LintFinding]:
    """Lint a single preset YAML file.

    The raw-document checks run FIRST, on the YAML tree, so a preset that does not load is
    still diagnosed: the parser raises on the first problem it meets and reports nothing else,
    which for a genuinely broken preset is the least useful moment to go quiet.
    """
    path = Path(path)
    label = str(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [LintFinding(ERROR, "yaml-error", f"cannot read YAML: {exc}", label)]
    findings = check_raw_document(raw, label)
    try:
        spec = preset_spec_from_yaml(raw, path)
    except ValueError as exc:
        # The parser's message already carries the path; keep it as the single error, ON TOP
        # of whatever the structural pass already explained.
        findings.append(LintFinding(ERROR, "parse-error", str(exc), label))
        return findings
    return findings + lint_spec(spec, preset_label=label)


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
        _check_observations,
        _check_unfilled_placeholders,
        _check_literal_uuid_in_path,
        _check_multi_role,
        _check_free_text_vocabularies,
        _check_coordinates_have_a_referential,
        _check_zone_and_duration_shape,
        _check_record_uid_declared,
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
    for tmpl in spec.observations:
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


# --- the raw-document pass (runs BEFORE parsing) ------------------------------

def check_raw_document(raw: object, label: str) -> list[LintFinding]:
    """Structural checks over the YAML tree, before the parser gets a say.

    WHY a separate pass: ``preset_spec_from_yaml`` raises on the FIRST problem, so a preset
    with three mistakes reports one — and the mistakes that stop it loading are exactly the
    ones an author most needs spelled out. These checks name the fix instead.
    """
    if not isinstance(raw, dict):
        return []
    findings: list[LintFinding] = []
    findings.extend(_raw_top_level_keys(raw, label))
    findings.extend(_raw_conditional_links(raw, label))
    findings.extend(_raw_glob_in_selector_name(raw, label))
    return findings


def _raw_top_level_keys(raw: dict, label: str) -> list[LintFinding]:
    """An unknown top-level key — most often a model field that belongs under ``common:``."""
    out: list[LintFinding] = []
    for key in raw:
        name = str(key)
        if name in TOP_LEVEL_KEYS:
            continue
        if is_assignable(name):
            out.append(LintFinding(
                ERROR, "misplaced-top-level",
                f"{name!r} is a model field, not a top-level key; at the top level its "
                f"mapping is never read", label, name,
                suggestion=f"common:\n            {name}: {_yaml_scalar(raw[key])}"))
            continue
        close = difflib.get_close_matches(name, sorted(TOP_LEVEL_KEYS), n=1)
        hint = f"did you mean {close[0]!r}?" if close else \
            f"admitted keys: {sorted(TOP_LEVEL_KEYS)}"
        out.append(LintFinding(
            ERROR, "unknown-top-level", f"unknown top-level key {name!r}", label, name,
            suggestion=hint))
    return out


def _raw_conditional_links(raw: dict, label: str) -> list[LintFinding]:
    """A conditional under ``links:``. The short block takes BARE ENUM TOKENS only; a value
    that must be computed is mapped by its full column name under ``position:``."""
    out: list[LintFinding] = []
    for index, observation in enumerate(raw.get("observations") or []):
        if not isinstance(observation, dict):
            continue
        links = observation.get("links")
        if not isinstance(links, dict):
            continue
        for key, value in links.items():
            if not is_condition(value):
                continue
            column = LINK_FIELDS.get(str(key), f"{key}_link")
            out.append(LintFinding(
                ERROR, "conditional-in-links",
                f"links.{key} holds a conditional; the links block takes bare enum tokens "
                f"only", label, f"observations[{index}].links.{key}",
                suggestion=f"move it under position: as the full column name —\n"
                           f"            {column}: {value}"))
    return out


def _raw_glob_in_selector_name(raw: dict, label: str) -> list[LintFinding]:
    """``name:`` is EXACT basename equality, never a glob — a wildcard there matches nothing,
    so the preset silently never fires. ``path:`` is the anchored form with wildcards."""
    selectors = raw.get("input_selector")
    entries = selectors if isinstance(selectors, list) else [selectors]
    out: list[LintFinding] = []
    for index, selector in enumerate(entries):
        if not isinstance(selector, dict):
            continue
        name = selector.get("name")
        if isinstance(name, str) and any(ch in name for ch in "*?["):
            where = f"input_selector[{index}].name" if len(entries) > 1 else "input_selector.name"
            out.append(LintFinding(
                WARNING, "glob-in-selector-name",
                f"selector name {name!r} contains wildcards, but 'name' is matched as an "
                f"EXACT basename — this preset will never match a file", label, where,
                suggestion=f"name: {name.lstrip('*')}   # the exact basename, or use "
                           f"path: for an anchored path with {{uuid}} / * segments"))
    return out


def _yaml_scalar(value: object) -> str:
    """Render a YAML value back for a suggestion snippet (a mapping keeps its flow form)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
    return "" if value is None else str(value)


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
    for tmpl in spec.observations:
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


def _check_observations(spec: PresetSpec, label: str) -> list[LintFinding]:
    out: list[LintFinding] = []
    for index, tmpl in enumerate(spec.observations):
        where = f"observations[{index}]"
        fields = {f.model_field for f in tmpl.fields}
        # Half a coordinate is almost always a mistake.
        if ("latitude" in fields) != ("longitude" in fields):
            out.append(LintFinding(
                WARNING, "half-coordinate",
                "only one of latitude/longitude is mapped — a point needs both; map "
                "the missing one or drop the partial coordinate", label, f"{where}.position"))
        # An observation's three corner links are its semantics; a missing one leaves that
        # corner's meaning undefined. Keyed by the `links:` YAML key, with the column each
        # maps to (LINK_FIELDS) — so the message names what the author actually writes.
        # A template with no position (in it or in `common:`) has no position whose use a
        # spatial link could describe (decision 55): one is neither expected nor kept.
        has_position = any(f.model_field in V.STANDARD_SPATIAL_PARTS
                            for f in (*spec.common, *tmpl.fields))
        _link_example = {"entity": "subject", "spatial": "measured", "temporal": "at_least_once"}
        for key, column in LINK_FIELDS.items():
            if column == "spatial_link" and not has_position:
                if column in fields:
                    out.append(LintFinding(
                        WARNING, "spatial-link-without-position",
                        "observation maps no position, so its spatial link describes nothing "
                        "and ingest drops it — remove it, or map the position", label,
                        f"{where}.links"))
                continue
            if column not in fields:
                out.append(LintFinding(
                    WARNING, "missing-link",
                    f"observation declares no {key} link — that corner's meaning is undefined; "
                    f"add it under links (e.g. {key}: {_link_example[key]}) so the row is "
                    "interpretable", label, f"{where}.links"))
        # Bearer fields the entity link will never write (decision 55): `subject` uses the
        # targeted entity itself, `unknown` has no bearer by definition. Ingest drops them;
        # saying so here catches the preset whose author meant a different link.
        # The link's VALUE is read from the field specs, and both it and the bearer fields
        # may be mapped in `common:` as well as in the template.
        entity_link = next(
            (f.ref.arg for f in (*tmpl.fields, *spec.common)
             if f.model_field == "entity_link" and f.ref is not None and f.ref.kind == "const"),
            None)
        bearer_mapped = any(f.model_field.startswith("bearer_")
                            for f in (*spec.common, *tmpl.fields))
        if bearer_mapped and entity_link in (V.EntityLink.SUBJECT.value,
                                             V.EntityLink.UNKNOWN.value):
            out.append(LintFinding(
                WARNING, "bearer-without-party",
                f"entity link is `{entity_link}`, so no separate bearer is written — the "
                "mapped bearer_* fields will be silently dropped", label, f"{where}.links"))
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
    for index, tmpl in enumerate(spec.observations):
        for field in tmpl.fields:
            addspec(field, f"observations[{index}].{field.model_field}")
        for time in tmpl.temporal:
            addref(time.lower, f"observations[{index}].time")
            addref(time.upper, f"observations[{index}].time")
            if time.format:
                out.append((time.format, f"observations[{index}].time.format"))
            if time.zone is not None:
                addspec(time.zone, f"observations[{index}].time.zone")
            for override in time.overrides:
                addspec(override, f"observations[{index}].{override.model_field}")
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


# --- silent-degradation checks (the preset LOADS, then quietly does nothing) --------

# The four columns whose vocabulary is NOT validated at load: they are free TEXT, so an
# unrecognised value is written happily and then simply never normalised / never integrated.
# That silence is what this check exists to break — it is the widest gap in the language,
# because every other controlled field raises at load or at the DDL CHECK.
_FREE_TEXT_VOCABULARIES: dict[str, frozenset[str]] = {
    "entity_type": frozenset(e.value for e in V.EntityType),
    "identifier_type": frozenset(e.value for e in V.IdentifierType),
    "spatial_referential": frozenset(e.value for e in V.SpatialReferential),
}


def _all_field_specs(spec: PresetSpec) -> list[tuple[FieldSpec, str]]:
    """Every mapped FieldSpec with a location label — ``common:`` plus each observation's
    ``position:`` and temporal overrides."""
    out: list[tuple[FieldSpec, str]] = [(f, "common") for f in spec.common]
    for index, tmpl in enumerate(spec.observations):
        out.extend((f, f"observations[{index}].position") for f in tmpl.fields)
        for time in tmpl.temporal:
            out.extend((f, f"observations[{index}].time") for f in time.overrides)
    return out


def _check_free_text_vocabularies(spec: PresetSpec, label: str) -> list[LintFinding]:
    """A const on a free-text controlled column that is not a recognised member."""
    out: list[LintFinding] = []
    for field, where in _all_field_specs(spec):
        allowed = _FREE_TEXT_VOCABULARIES.get(field.model_field)
        if allowed is None or field.ref is None or field.ref.kind != "const":
            continue
        value = str(field.ref.arg)
        if value in allowed:
            continue
        close = difflib.get_close_matches(value, sorted(allowed), n=1)
        out.append(LintFinding(
            WARNING, "free-text-vocab",
            f"{field.model_field} = const({value}) is not a recognised value; this column is "
            f"free text, so nothing rejects it — the value is simply never interpreted",
            label, f"{where}.{field.model_field}",
            suggestion=(f"{field.model_field}: const({close[0]})" if close
                        else f"one of: {sorted(allowed)}")))
    return out


def _check_coordinates_have_a_referential(spec: PresetSpec, label: str) -> list[LintFinding]:
    """Coordinates with no datum. The parts are datum-AGNOSTIC by design, so without an
    explicit referential the spatial expression resolves to ``unknown`` and the coordinates
    are never integrated — the row keeps its raw lat/lon and gains no place."""
    out: list[LintFinding] = []
    common = {f.model_field for f in spec.common}
    for index, tmpl in enumerate(spec.observations):
        mapped = {f.model_field for f in tmpl.fields} | common
        if not ({"latitude", "longitude"} & mapped) or "spatial_referential" in mapped:
            continue
        out.append(LintFinding(
            WARNING, "coords-no-referential",
            "coordinates are mapped but no spatial_referential is declared; the datum is "
            "never inferred, so these coordinates are NOT integrated",
            label, f"observations[{index}].position",
            suggestion="spatial_referential: const(epsg:4326)"))
    return out


def _check_zone_and_duration_shape(spec: PresetSpec, label: str) -> list[LintFinding]:
    """Two advisory nudges on the time block: a fixed offset where a zone name would resolve
    DST properly, and a duration bound with no role on its derived end."""
    out: list[LintFinding] = []
    for index, tmpl in enumerate(spec.observations):
        for time in tmpl.temporal:
            where = f"observations[{index}].time"
            zone = time.zone
            if (zone is not None and zone.ref is not None and zone.ref.kind == "const"
                    and isinstance(zone.ref.arg, str)):
                token = parse_zone_token(zone.ref.arg)
                if (token is not None and token.zone_name is None
                        and token.base_offset_hours not in (None, 0.0)):
                    out.append(LintFinding(
                        ADVICE, "fixed-offset-zone",
                        f"zone const({zone.ref.arg}) is a FIXED offset, applied season-blind; "
                        f"a source that observes daylight saving is then an hour out for half "
                        f"the year", label, f"{where}.zone",
                        suggestion="zone: const(Europe/Zurich)   # name the IANA zone so each "
                                   "bound resolves its own offset"))
            if time.duration_unit is None:
                continue
            hint = _unit_hint_from_name(time.upper.arg if time.upper is not None else None)
            if hint is not None and hint != time.duration_unit:
                out.append(LintFinding(
                    ADVICE, "duration-unit-mismatch",
                    f"{DURATION_BOUND_NAME} declares unit: {time.duration_unit} but the source "
                    f"field {time.upper.arg!r} reads as {hint}; a wrong unit mis-scales every "
                    f"derived end bound", label, where,
                    suggestion=f"unit: {hint}   # if the column really is {hint}"))
    return out


# Column-name endings that betray the unit a duration column is really in.
_UNIT_HINTS: tuple[tuple[str, str], ...] = (
    ("_ms", "ms"), ("millis", "ms"), ("_us", "us"), ("micros", "us"),
    ("_min", "min"), ("minutes", "min"), ("_h", "h"), ("hours", "h"),
)


def _unit_hint_from_name(field_name: object) -> str | None:
    """The unit a column NAME implies, or None when it implies nothing."""
    if not isinstance(field_name, str):
        return None
    lowered = field_name.casefold()
    for ending, unit in _UNIT_HINTS:
        if lowered.endswith(ending):
            return unit
    return None


def _check_record_uid_declared(spec: PresetSpec, label: str) -> list[LintFinding]:
    """Neither a mapped uid nor the ``default`` token. Both give the same generated UID, so
    this is purely about intent: a reviewer cannot tell a considered "this source has no
    stable key" from a mapping the author forgot."""
    if spec.source_record_uid is not None or spec.source_record_uid_default:
        return []
    return [LintFinding(
        ADVICE, "implicit-record-uid",
        "no source_record_uid: the engine generates one, but say so — omission and oversight "
        "read identically in the file", label, "source_record_uid",
        suggestion="source_record_uid: default   # or field(\"Item ID\") when the source has "
                   "a real key")]
