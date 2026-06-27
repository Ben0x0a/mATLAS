"""File-centric preset matching.

Defines:    detect_file_format (open-once magic detection of a SourceFile) and
            match_file (path/name pre-filter -> type guard -> structural tie-break).
Used by:    the pipeline.
Depends on: sources (Container/SourceFile, pathmatch, format_detect), presets.spec.

Iteration is per file: each SourceFile is matched independently, so a selector that fans
out (a ``name`` present in many folders, or a ``{uuid}`` path) applies the preset to each
matching file. Same-role selectors are OR alternatives; the first to match supplies the
reader params.
"""
from __future__ import annotations

import logging
import zipfile
from typing import Callable

from model_atlas.presets.spec import InputSelector, PresetSpec
from model_atlas.sources.container import SourceFile
from model_atlas.sources.format_detect import detect_format
from model_atlas.sources.pathmatch import name_matches, path_matches

log = logging.getLogger(__name__)

_HEAD = 512

PeekFn = Callable[[SourceFile, InputSelector], "set[str] | None"]


def detect_file_format(file: SourceFile) -> str | None:
    """Open the file once and magic-detect its format (a few KB + a zip central-dir peek)."""
    container = file.container
    try:
        with container.open(file) as fh:
            head = fh.read(_HEAD)
    except (OSError, KeyError, zipfile.BadZipFile):
        log.debug(f"Could not read {file.logical_path} for format detection", exc_info=True)
        return None

    def peek_zip() -> list[str]:
        try:
            with container.open(file) as fh:
                with zipfile.ZipFile(fh) as zf:
                    return zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return []

    return detect_format(head, peek_zip, file.logical_path.suffix)


def _file_namespace(file: SourceFile) -> str | None:
    """Sniff the XML root namespace (the dialect discriminator) from the file's head."""
    from model_atlas.sources.readers.xml_reader import sniff_namespace

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


def _structural_score(preset: PresetSpec, columns: set[str]) -> int:
    """+1 per declared column present, -2 per absent. Uses ``expected_columns`` when set,
    else the columns the mapping references. Imported lazily to avoid a module cycle."""
    from model_atlas.reporting import _column_refs
    from model_atlas.transforms.assemble import make_resolver

    signature = list(preset.expected_columns) or _column_refs(preset)
    resolve = make_resolver(sorted(columns))
    present = absent = 0
    for ref in signature:
        if resolve(ref) is not None:
            present += 1
        else:
            absent += 1
    return present - 2 * absent


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
    """Pick the best (preset, selector) for one sub-unit by structural column fit."""
    if len(group) == 1 or peek is None:
        return group[0]
    scored: list[tuple[int, int, tuple[PresetSpec, InputSelector]]] = []
    for order, (preset, selector) in enumerate(group):
        columns = peek(file, selector)
        score = _structural_score(preset, columns) if columns is not None else 0
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
    """Required columns the preset maps that are absent from the source (empty if it fits).

    Returns empty when columns can't be peeked (no peek fn / peek failed) — we never
    disqualify on an inability to verify, only on confirmed absence."""
    if peek is None:
        return set()
    columns = peek(file, selector)
    if columns is None:
        return set()
    from model_atlas.reporting import required_columns

    return required_columns(preset) - set(columns)


def match_files_detailed(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    peek: PeekFn | None = None,
) -> tuple[list[tuple[PresetSpec, InputSelector]], list[tuple[PresetSpec, InputSelector, set[str]]]]:
    """Match a SourceFile, returning (matches, unsatisfied).

    1. Location pre-filter (name-only); 2. type/namespace guard; 3. applicability gate — a
    preset only matches if ALL its REQUIRED (mapped, non-optional) columns are present;
    4. one winner per distinct in-file unit (model/table/sheet) via structural tie-break.

    ``unsatisfied`` lists (preset, selector, missing_columns) for a unit that WAS located and
    type-matched but where no preset's required columns are satisfied — a faithful mapping is
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
    detected = detect_file_format(file)
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


def match_files(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    peek: PeekFn | None = None,
) -> list[tuple[PresetSpec, InputSelector]]:
    """The matched (preset, selector) pairs for a file (see match_files_detailed)."""
    return match_files_detailed(file, presets, root_prefix_depth=root_prefix_depth, peek=peek)[0]


def match_file(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    peek: PeekFn | None = None,
) -> tuple[PresetSpec, InputSelector] | None:
    """Back-compat single-match: the first of ``match_files`` (or None)."""
    matches = match_files(file, presets, root_prefix_depth=root_prefix_depth, peek=peek)
    return matches[0] if matches else None
