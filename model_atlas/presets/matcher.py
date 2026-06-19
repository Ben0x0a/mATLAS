"""Preset matching for discovered source elements."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.presets.spec import PresetSpec

log = logging.getLogger(__name__)


def _matches_filename(selector: dict[str, Any], path: Path) -> bool:
    file_name = selector.get("file_name")
    return not file_name or path.name == file_name


def _matches_element_filename(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    file_name = selector.get("file_name")
    if not file_name:
        return True
    candidates = {
        element.path.name,
        element.logical_name,
        Path(element.source_original_path).name,
    }
    return str(file_name) in candidates


def _normalise_relpath(value: str) -> str:
    """POSIX separators, no leading slash — so a UNIX-absolute ``/private/.../db``
    and a ZIP entry stored as ``private/.../db`` compare equal."""
    return value.replace("\\", "/").lstrip("/")


def _relpath_matches(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    db_relpath = selector.get("db_relpath")
    return bool(db_relpath) and (
        _normalise_relpath(element.source_original_path) == _normalise_relpath(str(db_relpath))
    )


def _matches_sqlite(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    if selector.get("source_type") != ElementType.SQLITE.value:
        return False
    file_name = selector.get("file_name")
    db_relpath = selector.get("db_relpath")
    if not file_name and not db_relpath:
        return True  # type-only selector

    # Context-aware criterion: a selector may declare BOTH file_name and db_relpath,
    # and the one relevant to the source's origin is applied.
    #   - Container source (SQLite inside an archive, e.g. a ZIP): the discovered
    #     element carries the internal acquisition path, so db_relpath is the
    #     discriminator. Matching on a bare file name would be unsafe — an iOS dump
    #     holds many unrelated "Cache.sqlite" files at different paths.
    #   - Direct file/folder source: there is no internal path, so file_name is the
    #     discriminator.
    # A container element is recognised by its original path differing from the
    # on-disk path of the archive it was discovered in. The check is archive-format
    # agnostic; ZIP is the only container format supported today.
    is_container = element.source_original_path != str(element.path)
    if is_container:
        return _relpath_matches(selector, element) if db_relpath else _matches_element_filename(selector, element)
    return _matches_element_filename(selector, element) if file_name else _relpath_matches(selector, element)


def _matches_excel(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    if selector.get("source_type") != ElementType.EXCEL.value:
        return False
    if not _matches_filename(selector, element.path):
        return False
    sheet_name = selector.get("sheet_name")
    return not sheet_name or element.sheet_name == sheet_name


def _matches_csv(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    return selector.get("source_type") == ElementType.CSV.value and _matches_filename(selector, element.path)


def _selector_matches(selector: dict[str, Any], element: DiscoveredElement) -> bool:
    if element.source_type == ElementType.SQLITE:
        return _matches_sqlite(selector, element)
    if element.source_type == ElementType.EXCEL:
        return _matches_excel(selector, element)
    if element.source_type == ElementType.CSV:
        return _matches_csv(selector, element)
    return False


def _structural_score(preset: PresetSpec, columns: set[str]) -> int:
    """Score a candidate by how well its declared columns fit the source: +1 per
    column present, -2 per column absent. Higher = better fit. Uses the preset's
    declared ``expected_columns`` inventory when present (the strongest signature),
    else the columns the mapping references.
    Importing here avoids a module cycle (reporting imports assemble imports spec)."""
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


def match_preset(
    element: DiscoveredElement,
    presets: list[PresetSpec],
    *,
    peek_columns: "Callable[[DiscoveredElement, PresetSpec], set[str] | None] | None" = None,
) -> tuple[PresetSpec, dict[str, Any]] | None:
    """Match an element to a preset. When several presets match the coarse selector,
    tie-break by structural fit (which preset's mapped columns the source actually has)
    using ``peek_columns``; ties fall back to declaration order."""
    candidates: list[tuple[PresetSpec, dict[str, Any]]] = []
    for preset in presets:
        for selector in preset.selectors:
            if _selector_matches(selector, element):
                candidates.append((preset, selector))
                break
    if not candidates:
        log.debug("No preset selector matched source element: %s", element.source_file)
        return None
    if len(candidates) == 1 or peek_columns is None:
        return candidates[0]

    # Several presets match the same source — score by structural fit.
    scored: list[tuple[int, int, tuple[PresetSpec, dict[str, Any]]]] = []
    for order, (preset, selector) in enumerate(candidates):
        columns = peek_columns(element, preset)
        score = _structural_score(preset, columns) if columns is not None else 0
        scored.append((score, -order, (preset, selector)))
    best_score, _, best = max(scored)
    runner_up = sorted((s for s, _, _ in scored), reverse=True)
    if len(runner_up) > 1 and runner_up[0] == runner_up[1]:
        log.warning(
            "Ambiguous preset match for %s: tie at structural score %d; chose %s by declaration order",
            element.source_file, best_score, best[0].name,
        )
    log.debug("Tie-break chose preset %s (score=%d) for %s", best[0].name, best_score, element.source_file)
    return best
