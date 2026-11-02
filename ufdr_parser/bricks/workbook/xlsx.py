"""Stream sheets into an .xlsx within Excel's limits, without ever altering a value silently.

Defines:    SheetWriter (row sink for one logical sheet, rolling over to continuation sheets),
            open_workbook / cell helpers, and the Excel limit constants.
Used by:    workbook (the orchestrator).
Depends on: openpyxl (write-only mode).

HOW each value is written: as a TEXT cell, always. WHY: report values are evidence, not
input — a message body starting with ``=`` must never become a live formula (formula
injection), and an identifier such as ``+41791234567`` or ``007`` must never be re-typed as a
number. Two Excel limits are handled explicitly rather than by crashing or dropping data:

- More than 1,048,576 rows: the sheet continues on ``<name> (2)``, ``<name> (3)``, ...
- More than 32,767 characters in a cell: the text is cut and ends with an explicit marker
  stating its full length; the full value stays in the raw CSV. Excel counts UTF-16 units, so
  an emoji counts for two: counting Python characters let over-long cells through.

Characters that XML 1.0 cannot carry (and the XML writer therefore rejects, failing the whole
workbook) are replaced with a visible ``[U+XXXX]`` marker instead of being dropped. Every such
adjustment is counted and reported on the About sheet.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

log = logging.getLogger(__name__)

# A progress line per sheet at most this often, checking the clock every _PROGRESS_CHECK_EVERY
# rows. WHY: a sheet of millions of rows takes minutes and logged nothing until done, so the
# workbook phase looked frozen.
_PROGRESS_INTERVAL_S = 10.0
_PROGRESS_CHECK_EVERY = 5000

# Excel's hard limits (Excel specifications and limits; .xlsx format).
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_CELL_CHARS = 32_767
EXCEL_MAX_SHEET_NAME = 31

# What XML 1.0 excludes. WHY not openpyxl's ILLEGAL_CHARACTERS_RE: it omits the surrogates and
# U+FFFE / U+FFFF, on which saving then raises.
_ILLEGAL_XML_CHARACTERS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_TRUNCATION_MARKER = " [... cut by Excel's {limit} character cell limit; full text ({length} characters) is in the CSV]"
_HEADER_FONT = Font(bold=True)
_ALERT_FONT = Font(bold=True, color="B91C1C")
_WRAP = Alignment(wrap_text=True, vertical="top")
# Column width bounds (characters) when sizing from the header and first rows.
_MIN_WIDTH = 8
_MAX_WIDTH = 60
_WIDTH_SAMPLE_ROWS = 200


@dataclass
class WriteStats:
    """Adjustments made while writing, reported on the About sheet."""

    truncated_cells: int = 0
    escaped_characters: int = 0


def open_workbook() -> Workbook:
    return Workbook(write_only=True)


def _escape_illegal(text: str, stats: WriteStats) -> str:
    def replace(match: re.Match[str]) -> str:
        stats.escaped_characters += 1
        return f"[U+{ord(match.group(0)):04X}]"

    return _ILLEGAL_XML_CHARACTERS.sub(replace, text)


def _utf16_units(text: str) -> int:
    """The length Excel's cell limit counts (surrogates were escaped, so this cannot raise)."""
    return len(text.encode("utf-16-le")) // 2


def _cut_to_units(text: str, units: int) -> str:
    """``text`` cut to at most ``units`` UTF-16 units, never inside a surrogate pair."""
    return text.encode("utf-16-le")[: units * 2].decode("utf-16-le", "ignore")


def text_cell(sheet, value: object, stats: WriteStats, *, bold: bool = False, wrap: bool = False, alert: bool = False):
    """A write-only cell holding ``value`` as TEXT (never a formula/number), within limits."""
    if value is None:
        return None
    text = _escape_illegal(str(value), stats)
    # A text of limit/2 characters or fewer cannot exceed it: spares encoding nearly every cell.
    if len(text) > EXCEL_MAX_CELL_CHARS // 2 and (length := _utf16_units(text)) > EXCEL_MAX_CELL_CHARS:
        stats.truncated_cells += 1
        marker = _TRUNCATION_MARKER.format(limit=EXCEL_MAX_CELL_CHARS, length=length)
        text = _cut_to_units(text, EXCEL_MAX_CELL_CHARS - len(marker)) + marker
    cell = WriteOnlyCell(sheet, value=text)
    # Forced after assignment: openpyxl infers "f" for a leading "=" and must not here.
    cell.data_type = "s"
    if bold:
        cell.font = _HEADER_FONT
    if alert:
        cell.font = _ALERT_FONT
    if wrap:
        cell.alignment = _WRAP
    return cell


def continuation_name(base: str, part: int) -> str:
    """``base`` for part 1, then ``base (2)``, ``base (3)`` — kept within 31 characters."""
    if part == 1:
        return base[:EXCEL_MAX_SHEET_NAME]
    suffix = f" ({part})"
    return base[: EXCEL_MAX_SHEET_NAME - len(suffix)] + suffix


@dataclass
class SheetWriter:
    """Writes one logical sheet (header + rows), rolling over at Excel's row limit."""

    workbook: Workbook
    name: str
    header: list[str]
    stats: WriteStats
    sheet_names: list[str] = field(default_factory=list)  # mutable default — one list per writer
    row_count: int = 0
    _sheet: object = None
    _rows_in_sheet: int = 0
    _widths: list[int] = field(default_factory=list)  # mutable default — one list per writer
    # The current part's first rows, held back until its layout is written; None once it is.
    _sample: list[list[object]] | None = None
    _last_logged: float = field(default_factory=time.monotonic)

    def _new_sheet(self) -> None:
        self._finish_sheet()
        name = continuation_name(self.name, len(self.sheet_names) + 1)
        self._sheet = self.workbook.create_sheet(name)
        self.sheet_names.append(name)
        self._sheet.freeze_panes = "A2"
        self._widths = [len(column) for column in self.header]
        self._sample = []
        self._rows_in_sheet = 1

    def _write_sample(self) -> None:
        """Size the columns from the header and the sampled rows, then write them all.

        WHY rows are held back: a write-only sheet writes its layout with its first row and
        ignores any width set afterwards.
        """
        for index, width in enumerate(self._widths, start=1):
            self._sheet.column_dimensions[get_column_letter(index)].width = max(
                _MIN_WIDTH, min(_MAX_WIDTH, width + 2)
            )
        self._sheet.append([text_cell(self._sheet, column, self.stats, bold=True) for column in self.header])
        for values in self._sample:
            self._sheet.append([text_cell(self._sheet, value, self.stats) for value in values])
        self._sample = None

    def _finish_sheet(self) -> None:
        """Write what the current sheet part still holds back and set its filter range."""
        if self._sheet is None:
            return
        if self._sample is not None:
            self._write_sample()
        last_column = get_column_letter(max(1, len(self.header)))
        self._sheet.auto_filter.ref = f"A1:{last_column}{self._rows_in_sheet}"

    def append(self, values: Iterable[object]) -> None:
        if self._sheet is None or self._rows_in_sheet >= EXCEL_MAX_ROWS:
            self._new_sheet()
        values = list(values)
        if self._sample is None:
            self._sheet.append([text_cell(self._sheet, value, self.stats) for value in values])
        else:
            for index, value in enumerate(values[: len(self._widths)]):
                if value is not None:
                    self._widths[index] = max(self._widths[index], min(len(str(value)), _MAX_WIDTH))
            self._sample.append(values)
            if len(self._sample) >= _WIDTH_SAMPLE_ROWS:
                self._write_sample()
        self._rows_in_sheet += 1
        self.row_count += 1
        if self.row_count % _PROGRESS_CHECK_EVERY == 0 and time.monotonic() - self._last_logged >= _PROGRESS_INTERVAL_S:
            self._last_logged = time.monotonic()
            log.info(f"Writing workbook sheet {self.name}: {self.row_count} row(s) so far")

    def close(self) -> None:
        if self._sheet is None:
            self._new_sheet()  # an empty sheet still shows its header
        self._finish_sheet()
