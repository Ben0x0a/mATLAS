"""File-centric preset matching.

Defines:    match_files_detailed (path/name pre-filter -> type guard -> required-field
            gate -> structural tie-break, returning matches + unsatisfied units) and
            resolve_force_selector (force-preset in-file sub-unit resolution).
Used by:    the app-layer pipeline (``model_atlas.app.pipeline``).
Depends on: ingest (SourceFile, pathmatch, format_detect, readers.xml_reader for the
            namespace sniff), presets.spec, presets.fields (field_refs / make_resolver /
            required_fields / structural_score).

Iteration is per file: each SourceFile is matched independently, so a selector that fans
out (a ``name`` present in many folders, or a ``{uuid}`` path) applies the preset to each
matching file. Same-role selectors are OR alternatives; the first to match supplies the
reader params.

T60: matching now rides the ingest Container model, and the two reader touch points
(``peek`` for column peeks, ``subtables`` for force-mode sub-unit listing) are INJECTED
callables rather than reader imports — the pipeline decides how a peek is served (the
run store's already-ingested records), so the matcher stays free of any reader contract.
"""
from __future__ import annotations

import dataclasses
import logging
import zipfile
from typing import Callable

from model_atlas.ingest.containers import SourceFile
from model_atlas.ingest.format_detect import detect_source_format
from model_atlas.ingest.pathmatch import name_matches, path_matches
from model_atlas.presets.fields import (
    field_refs,
    make_resolver,
    required_fields,
    structural_score,
)
from model_atlas.presets.spec import InputSelector, PresetSpec

log = logging.getLogger(__name__)

PeekFn = Callable[[SourceFile, InputSelector], "set[str] | None"]
# Force-mode sub-unit listing: the in-file units (tables / sheets / model types) of one
# file, dispatched by the selector's format. Injected for the same reason as PeekFn.
SubtablesFn = Callable[[SourceFile, InputSelector], "list[str]"]


def _file_namespace(file: SourceFile) -> str | None:
    """Sniff the XML root namespace (the dialect discriminator) from the file's head."""
    from model_atlas.ingest.readers.xml_reader import sniff_namespace

    try:
        with file.container.open(file) as fh:
            head = fh.read(8192)
    except (OSError, KeyError, zipfile.BadZipFile):
        log.debug(f"Could not read {file.logical_path} for namespace sniff", exc_info=True)
        return None
    return sniff_namespace(head)


def _selector_location_matches(selector: InputSelector, file: SourceFile, root_prefix_depth: int) -> bool:
    if selector.name is not None:
        return name_matches(selector.name, file.logical_path)
    if selector.path is not None:
        return path_matches(selector.path, file.logical_path, root_prefix_depth=root_prefix_depth)
    return False


def _subunit_key(selector: InputSelector) -> tuple:
    """The in-file unit a selector targets. Selectors on DIFFERENT units of one file can ALL
    apply (e.g. one preset per XML model type, or per SQLite table); selectors on the SAME
    unit compete and are tie-broken to one."""
    if selector.format == "xml":
        return ("xml", selector.model)
    if selector.format == "sqlite":
        return ("sqlite", selector.table or ("sql", selector.sql))
    if selector.format == "excel":
        return ("excel", selector.sheet)
    return ("csv", None)  # the whole file is the unit


def _tie_break(
    group: list[tuple[PresetSpec, InputSelector]], file: SourceFile, peek: PeekFn | None
) -> tuple[PresetSpec, InputSelector]:
    """Pick the best (preset, selector) for one sub-unit by structural field fit."""
    if len(group) == 1 or peek is None:
        return group[0]
    scored: list[tuple[int, int, tuple[PresetSpec, InputSelector]]] = []
    for order, (preset, selector) in enumerate(group):
        columns = peek(file, selector)
        score = structural_score(preset, columns) if columns is not None else 0
        scored.append((score, -order, (preset, selector)))
    best_score, _, best = max(scored)
    ranked = sorted((s for s, _, _ in scored), reverse=True)
    if len(ranked) > 1 and ranked[0] == ranked[1]:
        log.warning(f"Ambiguous preset match for {file.logical_path}: tie at structural score {best_score}; chose {best[0].name} by declaration order")
    log.debug(f"Tie-break chose preset {best[0].name} (score={best_score}) for {file.logical_path}")
    return best


def _missing_required(
    preset: PresetSpec, file: SourceFile, selector: InputSelector, peek: PeekFn | None
) -> set[str]:
    """Required fields the preset maps that are absent from the source (empty if it fits).

    Returns empty when fields can't be peeked (no peek fn / peek failed) — we never
    disqualify on an inability to verify, only on confirmed absence."""
    if peek is None:
        return set()
    columns = peek(file, selector)
    if columns is None:
        return set()
    return required_fields(preset) - set(columns)


def match_files_detailed(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    peek: PeekFn | None = None,
) -> tuple[list[tuple[PresetSpec, InputSelector]], list[tuple[PresetSpec, InputSelector, set[str]]]]:
    """Match a SourceFile, returning (matches, unsatisfied).

    1. Location pre-filter (name or path); 2. type/namespace guard; 3. applicability gate — a
    preset only matches if ALL its REQUIRED (mapped, non-optional) fields are present;
    4. one winner per distinct in-file unit (model/table/sheet) via structural tie-break.

    ``unsatisfied`` lists (preset, selector, missing_fields) for a unit that WAS located and
    type-matched but where no preset's required fields are satisfied — a faithful mapping is
    impossible and the caller must surface it loudly (never silently drop it).
    """
    # Step 1 — location pre-filter, one selector per preset (first match wins; OR within role).
    located: list[tuple[PresetSpec, InputSelector]] = []
    for preset in presets:
        for selector in preset.input_selectors:
            if _selector_location_matches(selector, file, root_prefix_depth):
                located.append((preset, selector))
                break
    if not located:
        log.debug(f"No selector located {file.logical_path}")
        return [], []

    # Step 2 — type/namespace guard.
    detected = detect_source_format(file)
    file_namespace = _file_namespace(file) if detected == "xml" else None
    candidates: list[tuple[PresetSpec, InputSelector]] = []
    for preset, selector in located:
        if selector.format != detected:
            log.warning(f"format mismatch: {file.logical_path} is {detected}, preset {preset.name} expects {selector.format}; skipped")
            continue
        if detected == "xml" and selector.namespace is not None and selector.namespace != file_namespace:
            log.warning(f"namespace mismatch: {file.logical_path} is {file_namespace!r}, preset {preset.name} expects {selector.namespace!r}; skipped")
            continue
        candidates.append((preset, selector))

    # Steps 3 + 4 — per in-file unit: gate on required columns, then tie-break the qualified.
    groups: dict[tuple, list[tuple[PresetSpec, InputSelector]]] = {}
    for preset, selector in candidates:
        groups.setdefault(_subunit_key(selector), []).append((preset, selector))

    matches: list[tuple[PresetSpec, InputSelector]] = []
    unsatisfied: list[tuple[PresetSpec, InputSelector, set[str]]] = []
    for group in groups.values():
        qualified: list[tuple[PresetSpec, InputSelector]] = []
        disqualified: list[tuple[PresetSpec, InputSelector, set[str]]] = []
        for preset, selector in group:
            missing = _missing_required(preset, file, selector, peek)
            (disqualified if missing else qualified).append(
                (preset, selector, missing) if missing else (preset, selector)
            )
        if qualified:
            matches.append(_tie_break(qualified, file, peek))
            for preset, selector, missing in disqualified:
                log.info(f"{preset.name} not applied to {file.logical_path}: missing required "
                         f"{sorted(missing)}; another preset covered this unit")
        else:
            unsatisfied.extend(disqualified)
    return matches, unsatisfied


def resolve_force_selector(
    file: SourceFile,
    preset: PresetSpec,
    detected: str | None,
    *,
    subtables: SubtablesFn,
    peek: PeekFn,
) -> InputSelector:
    """Force mode: ignore location, but verify ``format`` and resolve the in-file sub-unit.

    Picks the first selector whose format matches the file (hard error on mismatch), then
    for excel/sqlite falls back to a structurally-best sheet/table when the declared one is
    absent (force-mode only — auto matching stays precise). ``subtables`` / ``peek`` are
    the caller-injected reader touch points (see the module header)."""
    selectors = [s for s in preset.input_selectors if s.format == detected]
    if not selectors:
        raise ValueError(
            f"force-preset format mismatch: {file.logical_path} is {detected}, "
            f"preset {preset.name!r} declares {[s.format for s in preset.input_selectors]}"
        )
    selector = selectors[0]
    if selector.format == "csv" or selector.sql:
        return selector  # csv: whole file; sql: run as written (a missing table just errors)

    declared = selector.sheet if selector.format == "excel" else selector.table
    units = subtables(file, selector)
    if declared in units:
        return selector

    # Choose the qualifying sub-unit (all referenced columns present) with the best score.
    refs = list(preset.expected_fields) or field_refs(preset)
    best: tuple[int, str] | None = None
    for sub in units:
        probe = dataclasses.replace(
            selector, **({"sheet": sub} if selector.format == "excel" else {"table": sub})
        )
        columns = peek(file, probe)
        if columns is None:
            continue
        resolve = make_resolver(sorted(columns))
        if all(resolve(ref) is not None for ref in refs):
            score = structural_score(preset, columns)
            if best is None or score > best[0]:
                best = (score, sub)
    if best is None:
        raise ValueError(
            f"force-preset {preset.name!r}: no {selector.format} sub-unit in "
            f"{file.logical_path} satisfies the mapped fields"
        )
    return dataclasses.replace(
        selector, **({"sheet": best[1]} if selector.format == "excel" else {"table": best[1]})
    )
