"""The export package: processing-artefact writers plus the relational export bricks.

Defines:    the pre-existing CSV/JSON sidecar writers and output-path helpers
            (``CSV_DELIMITER``, ``write_csv``, ``write_json``,
            ``traceability_path_for``, ``warnings_path_for``) that the old engine's
            ``pipeline`` and ``exporters.builtin`` still import, kept here so those
            call sites stay green while the new export bricks land.
Used by:    ``model_atlas.pipeline`` (sidecar paths + ``write_json``),
            ``model_atlas.exporters.builtin`` (``CSV_DELIMITER`` / ``write_csv``), and
            the new ``model_atlas.export.csv_flat`` brick (``CSV_DELIMITER``).
Depends on: pandas + the standard library only.

Why these helpers live in the package ``__init__`` (T50)
-------------------------------------------------------
T50 introduces the ``export`` PACKAGE (``export/csv_flat.py`` — the projection-driven
Exporter), but a standalone ``export.py`` module already carried these sidecar writers.
A package and a same-named module cannot coexist (the package always shadows the
module), so the module's content was moved here VERBATIM rather than duplicated: every
existing ``from model_atlas.export import ...`` keeps resolving to the same objects, so
the fenced old engine is untouched in behaviour. The init is deliberately kept to these
pre-existing helpers and does NOT import ``csv_flat`` — a submodule import would pull
pandas/projection into every ``import model_atlas.export`` and risk an import cycle, so
consumers import ``model_atlas.export.csv_flat`` explicitly.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

# Default CSV field delimiter for mATLAS output. ';' is the list separator Excel expects in many
# European locales, so a double-clicked CSV opens in columns without an import step. Consumed by
# write_csv (primary CSV), the exporters that emit CSV (e.g. geovisualizer), and export.csv_flat.
CSV_DELIMITER = ";"


def traceability_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.traceability.json")


def warnings_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.warnings.json")


def write_csv(df: pd.DataFrame, output_csv: Path) -> Path:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Writing CSV: path={output_csv} rows={len(df)} columns={len(df.columns)}")
    # QUOTE_NONNUMERIC quotes every non-numeric field, so values that contain the delimiter,
    # newlines, or a leading '=' (which spreadsheets would treat as a formula) can never break
    # the column structure of a forensic export. ';' delimiter opens straight into Excel columns.
    df.to_csv(output_csv, index=False, sep=CSV_DELIMITER, quoting=csv.QUOTE_NONNUMERIC)
    return output_csv


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Writing JSON sidecar: {path}")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
