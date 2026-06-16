"""Discover supported source elements from files or folders."""
from __future__ import annotations

import sqlite3
import zipfile
import logging
from pathlib import Path

import pandas as pd

from model_atlas.models import DiscoveredElement, ElementType

log = logging.getLogger(__name__)

_CSV_SUFFIXES = {".csv"}
_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            is_sqlite = f.read(16) == b"SQLite format 3\x00"
            log.debug("SQLite header check: path=%s is_sqlite=%s", path, is_sqlite)
            return is_sqlite
    except OSError:
        log.debug("SQLite header check failed: path=%s", path, exc_info=True)
        return False


def _discover_file(path: Path) -> list[DiscoveredElement]:
    suffix = path.suffix.casefold()
    log.debug("Discovering file: path=%s suffix=%s", path, suffix)
    if suffix in _CSV_SUFFIXES:
        log.debug("Discovered CSV source: %s", path)
        return [
            DiscoveredElement(
                source_type=ElementType.CSV,
                path=path,
                source_file=path.name,
                source_original_path=str(path),
                logical_name=path.name,
            )
        ]
    if suffix in _EXCEL_SUFFIXES:
        try:
            excel = pd.ExcelFile(path)
        except Exception:
            log.debug("Excel discovery failed: %s", path, exc_info=True)
            return []
        log.debug("Discovered Excel source: path=%s sheets=%s", path, excel.sheet_names)
        return [
            DiscoveredElement(
                source_type=ElementType.EXCEL,
                path=path,
                source_file=f"{path.name}::sheet={sheet}",
                source_original_path=str(path),
                logical_name=sheet,
                sheet_name=sheet,
            )
            for sheet in excel.sheet_names
        ]
    if _is_sqlite(path):
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            log.debug("SQLite read-only open failed during discovery: %s", path, exc_info=True)
            return []
        log.debug("Discovered SQLite source: %s", path)
        return [
            DiscoveredElement(
                source_type=ElementType.SQLITE,
                path=path,
                source_file=path.name,
                source_original_path=str(path),
                logical_name=path.name,
            )
        ]
    if zipfile.is_zipfile(path):
        elements: list[DiscoveredElement] = []
        try:
            with zipfile.ZipFile(path) as zipf:
                names = sorted(
                    name
                    for name in zipf.namelist()
                    if Path(name).suffix.casefold() in _SQLITE_SUFFIXES
                    and not name.endswith("/")
                )
        except (OSError, zipfile.BadZipFile):
            log.debug("ZIP discovery failed: %s", path, exc_info=True)
            return []
        log.debug("Discovered ZIP source: path=%s sqlite_entries=%s", path, names)
        for name in names:
            db_name = Path(name).name
            elements.append(
                DiscoveredElement(
                    source_type=ElementType.SQLITE,
                    path=path,
                    source_file=f"{path.name}::{name}",
                    source_original_path="/" + name.lstrip("/"),
                    logical_name=db_name,
                    preview_supported=False,
                )
            )
        return elements
    return []


def discover_elements(input_path: Path) -> list[DiscoveredElement]:
    input_path = Path(input_path)
    log.info("Discovering source elements under %s", input_path)
    if input_path.is_file():
        elements = _discover_file(input_path)
        log.info("Discovered %d element(s) from file %s", len(elements), input_path)
        return elements
    if not input_path.is_dir():
        raise FileNotFoundError(input_path)
    elements: list[DiscoveredElement] = []
    for path in sorted(input_path.rglob("*")):
        if not path.is_file():
            log.debug("Skipping non-file during discovery: %s", path)
            continue
        if path.name.endswith(("-wal", "-shm", "-journal")):
            log.debug("Skipping SQLite sidecar during discovery: %s", path)
            continue
        elements.extend(_discover_file(path))
    log.info("Discovered %d element(s) from folder %s", len(elements), input_path)
    return elements
