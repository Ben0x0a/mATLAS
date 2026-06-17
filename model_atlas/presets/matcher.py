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


def match_preset(element: DiscoveredElement, presets: list[PresetSpec]) -> tuple[PresetSpec, dict[str, Any]] | None:
    log.debug(
        "Matching source element against presets: type=%s source_file=%s original_path=%s",
        element.source_type.value,
        element.source_file,
        element.source_original_path,
    )
    for preset in presets:
        for selector in preset.selectors:
            log.debug(
                "Trying selector: preset=%s selector=%s source_file=%s",
                preset.name,
                selector,
                element.source_file,
            )
            if element.source_type == ElementType.SQLITE and _matches_sqlite(selector, element):
                log.debug("Matched SQLite selector: preset=%s source_file=%s", preset.name, element.source_file)
                return preset, selector
            if element.source_type == ElementType.EXCEL and _matches_excel(selector, element):
                log.debug("Matched Excel selector: preset=%s source_file=%s", preset.name, element.source_file)
                return preset, selector
            if element.source_type == ElementType.CSV and _matches_csv(selector, element):
                log.debug("Matched CSV selector: preset=%s source_file=%s", preset.name, element.source_file)
                return preset, selector
    log.debug("No preset selector matched source element: %s", element.source_file)
    return None
