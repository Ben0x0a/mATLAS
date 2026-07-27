"""CSV format reader (ingest brick): stdlib ``csv`` for verbatim text, physical-line
numbering from ``core/determinism.py``.

Defines:    ``CsvReader`` (format "csv").
Used by:    the ``format -> Reader`` registry (registered on import).
Depends on: ``model_atlas.core.determinism`` (``csv_data_line_numbers`` — rule 2),
            ``model_atlas.core.vocabularies`` (``RecordState``), ``model_atlas.ingest.
            readers.base``/``registry``, and the standard library ``csv`` module.

Ported from ``sources/readers/csv_reader.py`` (its ``delimiter``/``encoding``/
``skip_rows``/``header_row`` params are kept verbatim), with two additions this task
requires: the normative ``line_no`` assignment and verbatim values. WHY the stdlib ``csv`` module
replaces the old reader's ``pandas.read_csv``: pandas infers a column's dtype and would
re-format a textual value such as "46.193000" into the float 46.193, losing the trailing
zeros (canonical JSON rule 5 forbids this); ``csv.reader`` never coerces types, so every
field is the source text unchanged, with no ``parse_float`` shim needed. Using the stdlib
reader also gives direct access to physical lines, which ``csv_data_line_numbers`` needs.

KNOWN LIMITATION (disclosed, not covered by a test): rule 2 is stated in terms of physical
lines, and this reader parses one physical line per record — a value quoted across several
physical lines (a rare, non-forensic-standard CSV shape) will be mis-split. The old
pandas-based reader did not track line_no at all, so this is a new capability, not a
regression against previously-tested behaviour.
"""
from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from model_atlas.core.determinism import csv_data_line_numbers
from model_atlas.core.vocabularies import RecordState
from model_atlas.ingest.readers.base import RawRecord
from model_atlas.ingest.readers.registry import register_reader

log = logging.getLogger(__name__)


def _parse_line(line: str, delimiter: str) -> list[str]:
    """One physical line -> its fields, verbatim (never type-coerced)."""
    return next(csv.reader([line], delimiter=delimiter), [])


@register_reader
class CsvReader:
    format = "csv"

    def subunits(self, source: Path) -> list[str]:
        return []  # a CSV file is exactly one implicit subunit

    def iter_records(
        self, source: Path, subunit: str | None, params: dict[str, Any] | None = None,
    ) -> Iterator[RawRecord]:
        if subunit is not None:
            raise ValueError(f"csv has no subunits; got {subunit!r}")
        params = params or {}
        delimiter = params.get("delimiter", ",")
        encoding = params.get("encoding", "utf-8")
        skip_rows = int(params.get("skip_rows", 0) or 0)
        header_row = int(params.get("header_row", 0) or 0)
        # Rule 2's header_lines: every physical line up to and including the header line,
        # i.e. the skipped junk lines plus the header line itself.
        header_lines = skip_rows + header_row + 1

        # newline="": let the csv module see raw line endings; splitlines() below then
        # gives one element per physical line with the terminator stripped, matching what
        # csv_data_line_numbers expects (rule 2 numbers physical lines, not csv records).
        with source.open("r", encoding=encoding, newline="") as handle:
            physical_lines = handle.read().splitlines()

        header_index = skip_rows + header_row
        header_text = physical_lines[header_index] if header_index < len(physical_lines) else ""
        header = _parse_line(header_text, delimiter)

        for line_no, line in csv_data_line_numbers(physical_lines, header_lines=header_lines):
            row = _parse_line(line, delimiter)
            # Verbatim per column; a row shorter than the header leaves trailing columns
            # None (never dropped), a row longer than the header drops the unheadered
            # extras (there is no column name to key them under).
            fields = {name: (row[i] if i < len(row) else None) for i, name in enumerate(header)}
            yield RawRecord(line_no=line_no, fields=fields, recovery_state=RecordState.LIVE)

    def peek_fields(self, source: Path, subunit: str | None = None) -> set[str] | None:
        try:
            with source.open("rb") as raw:
                header_bytes = raw.readline()
            header = header_bytes.decode("utf-8-sig", errors="replace").rstrip("\r\n")
            return set(header.split(",")) if header else set()
        except OSError:
            log.debug(f"csv peek_fields failed for {source}", exc_info=True)
            return None
