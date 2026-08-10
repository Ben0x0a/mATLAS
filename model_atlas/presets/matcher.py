"""File-centric preset matching.

Defines:    locate_candidates (the content-free stage: path/name pre-filter -> type guard),
            match_files_detailed (that stage + required-field gate -> structural tie-break,
            returning matches + unsatisfied units) and resolve_force_selector (force-preset
            in-file sub-unit resolution).
Used by:    the app-layer pipeline (``model_atlas.app.pipeline``).
Depends on: ingest (SourceFile, pathmatch, format_detect, readers.xml_reader for the
            namespace sniff), presets.spec, presets.fields (field_refs / make_resolver /
            required_fields / structural_score).

Iteration is per file: each SourceFile is matched independently, so a selector that fans
out (a ``name`` present in many folders, or a ``{uuid}`` path) applies the preset to each
matching file. Same-role selectors are OR alternatives; the first to match supplies the
reader params.

Matching now rides the ingest Container model, and the two reader touch points
(``peek`` for column peeks, ``subtables`` for force-mode sub-unit listing) are INJECTED
callables rather than reader imports — the pipeline decides how a peek is served (the
run store's already-ingested records), so the matcher stays free of any reader contract.
"""
from __future__ import annotations

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

# ``locate_candidates(detected=...)`` sentinel: None is a MEANINGFUL detected format
# (an unrecognised file), so it cannot double as "not supplied — sniff it yourself".
_UNDETECTED = "\x00undetected"


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
    """The in-file unit a selector targets, as a grouping key. Selectors on DIFFERENT units
    of one file can ALL apply (e.g. one preset per XML model type, or per SQLite table);
    selectors on the SAME unit compete and are tie-broken to one.

    The unit itself comes from ``InputSelector.subunit`` (the one format->attribute mapping);
    only the ``sql:`` case is special here — a query names no stored unit, so each distinct
    query is its own group rather than colliding with every other query on ``None``."""
    unit = selector.subunit()
    if unit is None and selector.sql:
        return (selector.format, ("sql", selector.sql))
    return (selector.format, unit)


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
    # HOW: gate each required ref through the SAME glob-aware resolver assembly and structural
    # scoring use, counting a ref present when it resolves to a real column. WHY not a literal
    # ``required_fields(preset) - set(columns)``: a required ``field("ZLAT*")`` is not literally
    # in the column set, so the set difference would report it missing and disqualify the preset
    # even though the glob matches ``ZLATITUDE`` — silently dropping (or mis-ranking) the file.
    resolve = make_resolver(sorted(columns))
    return {ref for ref in required_fields(preset) if resolve(ref) is None}


def locate_candidates(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    detected: str | None = _UNDETECTED,
    warn: bool = True,
) -> list[tuple[PresetSpec, InputSelector]]:
    """Steps 1 + 2 of matching — location pre-filter, then type/namespace guard.

    Returns every (preset, selector) whose declared location AND format (plus, for xml, its
    namespace) fit the file. WHY it is public and separate from ``match_files_detailed``:
    these two steps read NO file content — only the path and a head-bytes magic sniff — so
    the pipeline can run them BEFORE ingest to decide which files an L0 walk needs to open
    at all. Step 3 (the required-column gate) is the one that needs records, and it only
    ever sees files this stage already kept.

    ``detected`` lets a caller that has already sniffed the format pass it in rather than
    re-opening the file; ``warn=False`` silences the mismatch warnings so the pre-ingest
    targeting pass does not duplicate the ones the real match emits.
    """
    # Step 1 — location pre-filter: keep EVERY selector matching the file's location, in
    # first-seen order. HOW: no ``break`` after the first hit. WHY: a preset may declare OR
    # alternatives (same role, sibling formats — e.g. ``export.*`` as both csv and sqlite) or
    # selectors for DIFFERENT in-file sub-units (two tables of one DB); keeping only the first
    # would let Step 2's format guard drop an OR sibling and would silently never extract every
    # sub-unit past the first. Grouped by preset so Step 2 can tell a genuine format mismatch
    # (no selector matches the format) from an OR sibling meant for another format.
    located: list[tuple[PresetSpec, list[InputSelector]]] = []
    for preset in presets:
        hits = [
            selector for selector in preset.input_selectors
            if _selector_location_matches(selector, file, root_prefix_depth)
        ]
        if hits:
            located.append((preset, hits))
    if not located:
        log.debug(f"No selector located {file.logical_path}")
        return []

    # Step 2 — type/namespace guard, per selector: keep every located selector whose format
    # (and, for xml, namespace) matches the file. A mismatched selector is only a genuine
    # mismatch worth warning about when NONE of the preset's located selectors match — an OR
    # sibling declared for another format/namespace is a legitimate alternative, not a mismatch.
    if detected is _UNDETECTED:
        detected = detect_source_format(file)
    file_namespace = _file_namespace(file) if detected == "xml" else None
    candidates: list[tuple[PresetSpec, InputSelector]] = []
    for preset, hits in located:
        format_ok = [selector for selector in hits if selector.format == detected]
        if detected == "xml":
            kept = [
                selector for selector in format_ok
                if selector.namespace is None or selector.namespace == file_namespace
            ]
        else:
            kept = format_ok
        if kept:
            candidates.extend((preset, selector) for selector in kept)
        elif format_ok:
            # Format matched but the xml namespace guard rejected every located selector.
            wanted = sorted({s.namespace for s in format_ok if s.namespace is not None})
            if warn:
                log.warning(f"namespace mismatch: {file.logical_path} is {file_namespace!r}, preset {preset.name} expects {wanted}; skipped")
        else:
            # The preset targeted this location but no selector matches the detected format.
            wanted = sorted({s.format for s in hits})
            if warn:
                log.warning(f"format mismatch: {file.logical_path} is {detected}, preset {preset.name} expects {wanted}; skipped")
    return candidates


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
    candidates = locate_candidates(file, presets, root_prefix_depth=root_prefix_depth)
    if not candidates:
        return [], []

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
    falls back to a structurally-best sub-unit when the declared table/sheet/model is absent
    (force-mode only — auto matching stays precise). Which attribute holds that unit is
    ``InputSelector.subunit`` / ``.retargeted``'s business, not this function's.
    ``subtables`` / ``peek`` are the caller-injected reader touch points (see the module
    header)."""
    selectors = [s for s in preset.input_selectors if s.format == detected]
    if not selectors:
        raise ValueError(
            f"force-preset format mismatch: {file.logical_path} is {detected}, "
            f"preset {preset.name!r} declares {[s.format for s in preset.input_selectors]}"
        )
    selector = selectors[0]
    if selector.format == "csv" or selector.sql:
        return selector  # csv: whole file; sql: run as written (a missing table just errors)

    units = subtables(file, selector)
    if selector.subunit() in units:
        return selector

    # Choose the qualifying sub-unit (all referenced columns present) with the best score.
    refs = list(preset.expected_fields) or field_refs(preset)
    best: tuple[int, str] | None = None
    for sub in units:
        probe = selector.retargeted(sub)
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
    return selector.retargeted(best[1])
