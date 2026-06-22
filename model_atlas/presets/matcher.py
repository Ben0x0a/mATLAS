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


def match_file(
    file: SourceFile,
    presets: list[PresetSpec],
    *,
    root_prefix_depth: int = 1,
    peek: PeekFn | None = None,
) -> tuple[PresetSpec, InputSelector] | None:
    """Match one SourceFile to a preset + the selector that claimed it, or None.

    1. Path/name pre-filter (cheap, name-only): collect (preset, selector) whose location
       key matches ``file.logical_path``. Same-role selectors are OR — keep the first match
       per preset.
    2. Type guard (open once): detect the file's format and drop candidates whose declared
       ``format`` disagrees, warning per dropped pairing.
    3. Tie-break: if more than one preset still matches, score by structural column fit.
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
        return None

    # Step 2 — type guard.
    detected = detect_file_format(file)
    candidates: list[tuple[PresetSpec, InputSelector]] = []
    for preset, selector in located:
        if selector.format != detected:
            log.warning(f"format mismatch: {file.logical_path} is {detected}, preset {preset.name} expects {selector.format}; skipped")
            continue
        candidates.append((preset, selector))
    if not candidates:
        return None
    if len(candidates) == 1 or peek is None:
        return candidates[0]

    # Step 3 — structural tie-break.
    scored: list[tuple[int, int, tuple[PresetSpec, InputSelector]]] = []
    for order, (preset, selector) in enumerate(candidates):
        columns = peek(file, selector)
        score = _structural_score(preset, columns) if columns is not None else 0
        scored.append((score, -order, (preset, selector)))
    best_score, _, best = max(scored)
    ranked = sorted((s for s, _, _ in scored), reverse=True)
    if len(ranked) > 1 and ranked[0] == ranked[1]:
        log.warning(f"Ambiguous preset match for {file.logical_path}: tie at structural score {best_score}; chose {best[0].name} by declaration order")
    log.debug(f"Tie-break chose preset {best[0].name} (score={best_score}) for {file.logical_path}")
    return best
