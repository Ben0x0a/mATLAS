"""The export package: the relational export bricks plus the run-sidecar helpers.

Defines:    ``CSV_DELIMITER`` (the one delimiter definition), ``write_json`` and the
            sidecar path helpers (``traceability_path_for`` / ``warnings_path_for``).
            The bricks live in the submodules (``csv_flat``, ``kml``, ``geojson``,
            ``xlsx``, ``geovisualizer``, ``sqlite_db``) behind ``registry``.
Used by:    ``model_atlas.app.pipeline`` (sidecar paths + ``write_json``) and
            ``model_atlas.export.csv_flat`` / ``geovisualizer`` (``CSV_DELIMITER``).
Depends on: the standard library only.

The init is deliberately kept to these small helpers and does NOT import ``csv_flat``
— a submodule import would pull pandas/projection into every
``import model_atlas.export`` and risk an import cycle, so consumers import
``model_atlas.export.csv_flat`` / ``.registry`` explicitly. (The old engine's
``write_csv`` flat-frame writer retired with it.)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Default CSV field delimiter for mATLAS output. ';' is the list separator Excel expects in many
# European locales, so a double-clicked CSV opens in columns without an import step. Consumed by
# export.csv_flat (primary CSV) and the exporters that emit CSV (e.g. geovisualizer).
CSV_DELIMITER = ";"


def traceability_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.traceability.json")


def warnings_path_for(output_csv: Path) -> Path:
    return output_csv.with_suffix(".matlas.warnings.json")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Writing JSON sidecar: {path}")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
