"""The combined-workbook brick: one .xlsx re-arranging the raw CSVs for analysts.

Defines:    WorkbookBrick (registered as "workbook"), build_workbook (stage -> resolve -> write),
            and the About (report-specific) and Documentation (static) sheet writers.
Used by:    ufdr_parser.bricks (registration), tests.
Depends on: bricks.base (DumpOutput), workbook.staging / links / comms / sheets / xlsx /
            documentation.

Flow: the dump's CSVs are staged into a temporary SQLite database (staging), every Party and
Attachment is folded into parent-keyed lookups (links), every communication is resolved once
(comms), then each sheet streams its rows into the workbook (sheets -> xlsx).

Two sheets explain the others. Documentation is STATIC — the same in every workbook — and
describes each sheet, each column and the general rules (from documentation.SHEET_DOCS).
About holds only what depends on this report: the report file, each sheet's row count, the
CSVs it was built from and the values Excel's limits forced to adjust; it is the first sheet
but written last, once those numbers are known. The file is written under a temporary name and renamed
when complete, so a crash never leaves a half-written workbook that looks finished.

Successor of the legacy UFEDParser ``output.xlsx`` (Locations / Communications / Identifiers /
CDRImporter), extended with per-category sheets, explicit offsets, provenance on every row and
a flagged — never silent — To inference.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from ufdr_parser.bricks.base import DumpOutput
from ufdr_parser.bricks.workbook.comms import build_comm_tables
from ufdr_parser.bricks.workbook.documentation import GENERAL_NOTES, SHEET_DOCS
from ufdr_parser.bricks.workbook.links import Links
from ufdr_parser.bricks.workbook.sheets import SHEET_BUILDERS, Context, Sheet
from ufdr_parser.bricks.workbook.staging import Staging
from ufdr_parser.bricks.workbook.xlsx import (
    SheetWriter,
    WriteStats,
    open_workbook,
    text_cell,
)
from ufdr_parser.const import COL_REPORT_ENTRY, COL_REPORT_FILE

log = logging.getLogger(__name__)

WORKBOOK_SUFFIX = "_combined.xlsx"
ABOUT_SHEET_NAME = "About"
DOCUMENTATION_SHEET_NAME = "Documentation"
APP_NAME = "ufdr_parser (mATLAS)"
_DISTRIBUTION_NAME = "model-atlas"


def _app_version() -> str:
    try:
        return metadata.version(_DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def workbook_path(output: DumpOutput) -> Path:
    return output.out_dir / f"{output.base}{WORKBOOK_SUFFIX}"


def build_workbook(output: DumpOutput) -> Path:
    """Write ``<base>_combined.xlsx`` next to the dump's CSVs; return its path."""
    target = workbook_path(output)
    temp_dir = Path(tempfile.mkdtemp(prefix="ufdr-workbook-"))
    conn = sqlite3.connect(temp_dir / "workbook.sqlite")
    # Throwaway staging DB: no durability needed, it is deleted at the end of the run.
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    try:
        staging = Staging(conn, output.group_paths)
        links = Links(staging)
        ctx = Context(
            staging, links, set(output.relations), build_comm_tables(staging, links), output.report_info_path
        )

        workbook = open_workbook()
        stats = WriteStats()
        about = workbook.create_sheet(ABOUT_SHEET_NAME)  # first in order, filled last
        _write_documentation(workbook.create_sheet(DOCUMENTATION_SHEET_NAME), stats)
        written: list[tuple[Sheet, SheetWriter]] = []
        for builder in SHEET_BUILDERS:
            sheet = builder(ctx)
            if sheet is None:
                continue
            writer = SheetWriter(workbook, sheet.doc.name, sheet.display, stats)
            for row in sheet.rows:
                writer.append(row)
            writer.close()
            written.append((sheet, writer))
            log.info(f"Workbook sheet {sheet.doc.name}: {writer.row_count} row(s)")
        _write_about(about, output, written, stats)

        partial = target.with_name(f".{target.name}.partial")
        log.info(f"Saving the workbook ({sum(w.row_count for _s, w in written)} row(s)) to {target}")
        workbook.save(partial)
        os.replace(partial, target)
        return target
    finally:
        conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _rows_writer(sheet, stats: WriteStats):
    def put(*values: object, bold: bool = False) -> None:
        sheet.append([text_cell(sheet, value, stats, bold=bold, wrap=True) for value in values])

    return put


def _write_about(sheet, output: DumpOutput, written: list[tuple[Sheet, SheetWriter]], stats: WriteStats) -> None:
    """What depends on THIS report: provenance of the workbook and what each sheet holds."""
    put = _rows_writer(sheet, stats)
    put("About this workbook", bold=True)
    put("Generated by", f"{APP_NAME} {_app_version()}")
    put("Generated at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    put("Report file", output.trace.get(COL_REPORT_FILE))
    put("Report entry", output.trace.get(COL_REPORT_ENTRY) or "(bare XML report)")
    put("Raw CSV folder", str(output.out_dir))
    put("How to read it", f"See the {DOCUMENTATION_SHEET_NAME} sheet: what every sheet and column means.")
    put("Cells cut at Excel's 32,767-character limit", str(stats.truncated_cells))
    put("Control characters shown as [U+XXXX]", str(stats.escaped_characters))
    put()
    put("Sheet", "Excel sheet(s)", "Rows", "Built from (CSV files in the raw CSV folder)", bold=True)
    for spec, writer in written:
        put(spec.doc.name, ", ".join(writer.sheet_names), str(writer.row_count), "\n".join(spec.built_from))
    built = {spec.doc.name for spec, _writer in written}
    for doc in SHEET_DOCS:
        if doc.name not in built:
            put(doc.name, "(not written)", "0", "The report holds no such records.")
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCD", (40, 30, 10, 70)):
        sheet.column_dimensions[letter].width = width


def _write_documentation(sheet, stats: WriteStats) -> None:
    """The static description of the workbook: identical whatever the report."""
    put = _rows_writer(sheet, stats)
    put("General rules", bold=True)
    for topic, text in GENERAL_NOTES:
        put(topic, text)
    put()
    put("Sheets", bold=True)
    put("Sheet", "One row is", "Notes", bold=True)
    for doc in SHEET_DOCS:
        put(doc.name, doc.row_is, "\n".join(doc.notes))
    put()
    put("Columns", bold=True)
    put("Sheet", "Column", "Origin", "Meaning", bold=True)
    for doc in SHEET_DOCS:
        for column in doc.columns:
            put(doc.name, column.display, column.origin, column.meaning)
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCD", (28, 45, 10, 100)):
        sheet.column_dimensions[letter].width = width


class WorkbookBrick:
    name = "workbook"
    description = "combined Excel workbook (communications, contacts, locations, identifiers, CDR)"

    def run(self, output: DumpOutput) -> list[Path]:
        return [build_workbook(output)]
