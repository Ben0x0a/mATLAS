"""The combined-workbook brick: one .xlsx re-arranging the raw CSVs for analysts.

Defines:    WorkbookBrick (registered as "workbook"), build_workbook (stage -> resolve -> write),
            and the About (report-specific) and Documentation (static) sheet writers.
Used by:    ufdr_parser.bricks (registration), tests.
Depends on: bricks.base (DumpOutput), workbook.config (ufdr_config.toml), workbook.staging /
            links / comms / sheets / xlsx / documentation.

Flow: the dump's CSVs are staged into a temporary SQLite database (staging), every Party and
Attachment is folded into parent-keyed lookups (links), every communication is resolved once
(comms), then each sheet streams its rows into the workbook (sheets -> xlsx).

WHAT is written comes from ufdr_config.toml (workbook.config): the data sheets and their order,
hidden columns, Contacts' child models, Locations' record types and the Identifiers sources.

Two sheets explain the others. Documentation describes each written sheet, each column and the
general rules (from documentation.SHEET_DOCS, for the configured sheets and visible columns) —
the same for every report under one configuration. About holds only what depends on this
report: the report file, the configuration file used, each sheet's row count, the CSVs it was
built from and the values Excel's limits forced to adjust; it is the first sheet but written
last, once those numbers are known. The file is written under a temporary name and renamed
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
from ufdr_parser.bricks.workbook.config import WorkbookConfig, load_workbook_config
from ufdr_parser.bricks.workbook.documentation import GENERAL_NOTES, SHEET_DOCS_BY_NAME
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


def check_config(config: WorkbookConfig) -> WorkbookConfig:
    """Reject sheet names the workbook does not know (a typo would silently drop a sheet)."""
    for where, names in (("[workbook].sheets", config.sheets), ("[hide_columns]", tuple(config.hide_columns))):
        unknown = [name for name in names if name not in SHEET_BUILDERS]
        if unknown:
            raise ValueError(
                f"ufdr_config.toml: unknown sheet(s) {unknown} in {where}; available: {list(SHEET_BUILDERS)}"
            )
    return config


def _hide(sheet: Sheet, hidden: frozenset[str]) -> Sheet:
    """The sheet without its hidden columns (header, display names and every row)."""
    if not hidden:
        return sheet
    keep = [index for index, name in enumerate(sheet.header) if name not in hidden]
    rows = sheet.rows
    return Sheet(
        sheet.doc,
        [sheet.header[i] for i in keep],
        [sheet.display[i] for i in keep],
        sheet.built_from,
        ([row[i] for i in keep] for row in rows),
    )


def build_workbook(output: DumpOutput) -> Path:
    """Write ``<base>_combined.xlsx`` next to the dump's CSVs; return its path."""
    target = workbook_path(output)
    config = check_config(load_workbook_config(output.config_path))
    temp_dir = Path(tempfile.mkdtemp(prefix="ufdr-workbook-"))
    conn = sqlite3.connect(temp_dir / "workbook.sqlite")
    # Throwaway staging DB: no durability needed, it is deleted at the end of the run.
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    try:
        staging = Staging(conn, output.group_paths)
        links = Links(staging)
        ctx = Context(
            staging,
            links,
            set(output.relations),
            build_comm_tables(staging, links),
            output.report_info_path,
            config,
        )

        workbook = open_workbook()
        stats = WriteStats()
        about = workbook.create_sheet(ABOUT_SHEET_NAME)  # first in order, filled last
        _write_documentation(workbook.create_sheet(DOCUMENTATION_SHEET_NAME), config, stats)
        written: list[tuple[Sheet, SheetWriter]] = []
        for name in config.sheets:
            sheet = SHEET_BUILDERS[name](ctx)
            if sheet is None:
                continue
            sheet = _hide(sheet, config.hide_columns.get(name, frozenset()))
            writer = SheetWriter(workbook, sheet.doc.name, sheet.display, stats)
            for row in sheet.rows:
                writer.append(row)
            writer.close()
            written.append((sheet, writer))
            log.info(f"Workbook sheet {sheet.doc.name}: {writer.row_count} row(s)")
        _write_about(about, output, config, written, stats)

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


def _write_about(
    sheet, output: DumpOutput, config: WorkbookConfig, written: list[tuple[Sheet, SheetWriter]], stats: WriteStats
) -> None:
    """What depends on THIS report: provenance of the workbook and what each sheet holds."""
    put = _rows_writer(sheet, stats)
    put("About this workbook", bold=True)
    put("Generated by", f"{APP_NAME} {_app_version()}")
    put("Generated at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    put("Case", output.case_name or "(none: outputs named after the report file)")
    put("Report file", output.trace.get(COL_REPORT_FILE))
    put("Report entry", output.trace.get(COL_REPORT_ENTRY) or "(bare XML report)")
    put("Raw CSV folder", str(output.out_dir))
    put("Workbook configuration", config.source)
    put("How to read it", f"See the {DOCUMENTATION_SHEET_NAME} sheet: what every sheet and column means.")
    put("Cells cut at Excel's 32,767-character limit", str(stats.truncated_cells))
    put("Control characters shown as [U+XXXX]", str(stats.escaped_characters))
    put()
    put("Sheet", "Excel sheet(s)", "Rows", "Built from (CSV files in the raw CSV folder)", bold=True)
    for spec, writer in written:
        put(spec.doc.name, ", ".join(writer.sheet_names), str(writer.row_count), "\n".join(spec.built_from))
    built = {spec.doc.name for spec, _writer in written}
    for name in config.sheets:
        if name not in built:
            put(name, "(not written)", "0", "The report holds no such records.")
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCD", (40, 30, 10, 70)):
        sheet.column_dimensions[letter].width = width


def _write_documentation(sheet, config: WorkbookConfig, stats: WriteStats) -> None:
    """The description of the configured sheets: identical whatever the report."""
    put = _rows_writer(sheet, stats)
    put("General rules", bold=True)
    for topic, text in GENERAL_NOTES:
        put(topic, text)
    put()
    put("Sheets", bold=True)
    put("Sheet", "One row is", "Notes", bold=True)
    docs = [SHEET_DOCS_BY_NAME[name] for name in config.sheets]
    for doc in docs:
        put(doc.name, doc.row_is, "\n".join(doc.notes))
    put()
    put("Columns", bold=True)
    put("Sheet", "Column", "Origin", "Meaning", bold=True)
    for doc in docs:
        hidden = config.hide_columns.get(doc.name, frozenset())
        for column in doc.columns:
            if column.name not in hidden:
                put(doc.name, column.display, column.origin, column.meaning)
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCD", (28, 45, 10, 100)):
        sheet.column_dimensions[letter].width = width


class WorkbookBrick:
    name = "workbook"
    description = "combined Excel workbook (sheets set in ufdr_config.toml)"

    def validate(self, config_path: Path | None) -> None:
        """Load and check ufdr_config.toml BEFORE the dump, so a typo fails in seconds."""
        check_config(load_workbook_config(config_path))

    def run(self, output: DumpOutput) -> list[Path]:
        return [build_workbook(output)]
