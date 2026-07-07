"""Write processing artefacts (CSV and JSON sidecars).

Defines:    output-path helpers and the CSV/JSON writers.
Used by:    pipeline, reporting.
Depends on: pandas, standard library.
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
# write_csv (primary CSV) and the exporters that emit CSV (e.g. geovisualizer).
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
