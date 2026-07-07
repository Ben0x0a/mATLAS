"""Autocompletion candidates for the run-level entity / linked_entity fields.

Defines:    PARAM_KEYWORDS (the call-form keyword templates the two fields accept) and
            param_completions(source_path) -> list[str], which always offers the keyword
            templates and, ONLY when the input is a single CSV file, also offers a
            ``field(<column>)`` entry per source column (a cheap header peek).
Used by:    gui.app (the QCompleter backing entityEdit / linkedEntityEdit).
Depends on: presets.expr (PARAM_EXPR_KINDS) + the stdlib csv sniffer.

Peeking is deliberately restricted to a single CSV file: a folder or archive would mean
walking many sources with differing columns — costly and ambiguous — so those inputs get
the keyword templates alone. Only the header line is read, and its delimiter is sniffed
so an Excel regional (semicolon) CSV lists its columns correctly.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from model_atlas.presets.expr import PARAM_EXPR_KINDS

log = logging.getLogger(__name__)

# Call-form templates for the reference kinds a run-level param may be, e.g. "field(".
PARAM_KEYWORDS: tuple[str, ...] = tuple(f"{kind}(" for kind in sorted(PARAM_EXPR_KINDS))

# Delimiters tried when sniffing a CSV header, commonest first (comma, then the Excel
# regional semicolon, then tab / pipe). Comma leads so it wins ties.
_CSV_DELIMITERS = ",;\t|"


def _is_single_csv(source_path: str | None) -> bool:
    """True when ``source_path`` points at a single existing .csv file (not a folder/archive)."""
    if not source_path:
        return False
    path = Path(source_path)
    return path.is_file() and path.suffix.lower() == ".csv"


def _sniff_header(header: str) -> list[str]:
    """Split a CSV header line, detecting its delimiter (comma / semicolon / tab / pipe).

    csv.Sniffer decides when it can; otherwise fall back to the delimiter that yields the
    most fields (comma wins ties). Parsing goes through csv.reader so quoted fields with an
    embedded delimiter stay intact."""
    try:
        delimiter = csv.Sniffer().sniff(header, delimiters=_CSV_DELIMITERS).delimiter
    except csv.Error:
        delimiter = max(_CSV_DELIMITERS, key=lambda d: len(next(csv.reader([header], delimiter=d))))
    fields = next(csv.reader([header], delimiter=delimiter))
    return [name.strip() for name in fields if name.strip()]


def _csv_columns(source_path: str) -> list[str]:
    """Column names of a single CSV from a cheap header read (one line), delimiter-sniffed.

    Returns [] on any failure — completion is a convenience and must never raise into the UI."""
    try:
        with open(source_path, encoding="utf-8-sig", errors="replace", newline="") as handle:
            header = handle.readline().rstrip("\r\n")
        return sorted(set(_sniff_header(header))) if header else []
    except Exception:  # noqa: BLE001 - a peek failure must never break the UI
        log.debug(f"column peek failed for {source_path!r}", exc_info=True)
        return []


def param_completions(source_path: str | None) -> list[str]:
    """Completion candidates for the entity / linked_entity fields.

    Always the keyword templates (``field(``, ``const(`` …); plus a ``field(<column>)``
    entry per column when ``source_path`` is a single CSV file."""
    candidates = list(PARAM_KEYWORDS)
    if _is_single_csv(source_path):
        candidates.extend(f"field({column})" for column in _csv_columns(source_path))
    return candidates
