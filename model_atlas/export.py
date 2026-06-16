"""Write processing artefacts (CSV and JSON sidecars).

Defines:    output-path helpers and the CSV/JSON writers.
Used by:    pipeline, reporting.
Depends on: pandas, standard library.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


def traceability_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.traceability.json")


def warnings_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.warnings.json")


def write_csv(df: pd.DataFrame, output_csv: Path) -> Path:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    log.info("Writing CSV: path=%s rows=%d columns=%d", output_csv, len(df), len(df.columns))
    df.to_csv(output_csv, index=False)
    return output_csv


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Writing JSON sidecar: %s", path)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
