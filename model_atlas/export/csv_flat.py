"""The csv_flat Exporter: the flat CSV view over the run store.

Defines:    the ``Exporter`` protocol (refactor_plan.md §3 — ``export(store, ctx)``),
            ``ExportContext`` (the output settings one export needs), and
            ``CsvFlatExporter`` — the shipped flat-CSV brick. It projects the run store
            through ``core.flat_view`` (one row per assertion, the authoritative
            ``docs/csv_migration.md`` column order plus the per-run ``orig_<col>`` tail)
            and writes it as a single ``utf-8-sig`` semicolon CSV (one file per format —
            the ``preset_id`` column separates presets for a caller who wants that).
Used by:    the app-layer pipeline (once T60 wires exporters after analysis) and
            tests/export/test_csv_flat.py.
Depends on: ``model_atlas.core.flat_view`` (``build_flat_view`` / ``CANONICAL_COLUMNS`` — the
            flat frame and its column contract), ``model_atlas.core.store`` (``RunStore``
            — read-only ``.connection`` here), ``model_atlas.export`` (``CSV_DELIMITER``
            — the one delimiter definition), pandas and the standard library.

Why the flat view and not a hand-built frame
-------------------------------------------
The flat CSV stopped being a 45-column dict assembled per assertion; it is now a
GENERATED query over the run database (docs/csv_migration.md). This brick therefore owns
no column knowledge of its own — the order, the numeric dtypes and the ``orig_<col>``
explosion all come from ``core.flat_view``. The four R6-mandatory columns
(``spatial_referential``, ``locates``, ``spatial_method``, ``temporal_method``) are part
of that contract, so a GPS fix, a geocoder centroid and a cell lookup each carry their
own referential + method and can never render identically.

Why utf-8-sig
-------------
The house rule: file output carries a BOM so Excel/Notepad auto-detect the encoding and
render accented cells correctly. (The old ``export.write_csv`` wrote plain utf-8; the
regenerated goldens are frozen as utf-8-sig, so the byte-parity oracle uses the BOM.)
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import pandas as pd

from model_atlas.core.flat_view import build_flat_view
from model_atlas.core.store import RunStore
from model_atlas.export import CSV_DELIMITER

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportContext:
    """Everything an export needs beyond the store: where to write and how.

    ``base_name`` is the output file's stem (one file per format — the ``preset_id``
    column separates presets in the single file). ``ranking_method`` pins which
    ``analysis`` method the flat view reads (None = the current/maximum method);
    ``include_source_columns`` toggles the ``orig_<col>`` tail.
    """

    output_dir: Path
    base_name: str = "merged"
    ranking_method: str | None = None
    include_source_columns: bool = True


@runtime_checkable
class Exporter(Protocol):
    """The output-brick contract (refactor_plan.md §3): read the store, write files.

    An exporter never writes to the store (it holds only ``.connection`` for reading),
    so principle 6 (analysis/derivation never blur) is preserved by construction.
    """

    format: str

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        ...


class CsvFlatExporter:
    """The flat-CSV export brick: project the store, write one CSV (or one per preset)."""

    format = "csv_flat"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        """Project the run store and write the flat CSV, returning the file written.

        HOW: build the flat frame ONCE via ``core.flat_view.build_flat_view`` (the authoritative
        column order + the per-run ``orig_<col>`` tail + restored numeric dtypes) and
        write it whole to ``<output_dir>/<base_name>.csv``. One file per format: the
        ``preset_id`` column already separates presets for a caller who needs it.
        """
        frame = build_flat_view(
            store.connection,
            include_source_columns=ctx.include_source_columns,
            ranking_method=ctx.ranking_method,
        )
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        return [self._write(frame, ctx.output_dir / f"{ctx.base_name}.csv")]

    def _write(self, frame: pd.DataFrame, path: Path) -> Path:
        """Write one frame as a utf-8-sig, ';'-delimited, QUOTE_NONNUMERIC CSV.

        QUOTE_NONNUMERIC quotes every non-numeric field, so a value containing the
        delimiter, a newline or a leading '=' (a spreadsheet formula trigger) can never
        break the column structure of a forensic export; the numeric-dtype columns the
        flat view restored stay unquoted so they read as numbers.
        """
        log.info(f"Writing flat CSV: path={path} rows={len(frame)} columns={len(frame.columns)}")
        frame.to_csv(
            path, index=False, sep=CSV_DELIMITER,
            quoting=csv.QUOTE_NONNUMERIC, encoding="utf-8-sig",
        )
        return path
