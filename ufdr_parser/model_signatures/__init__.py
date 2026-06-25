"""Model-drift checking for Cellebrite reports (NOT a model_atlas preset).

A standalone safeguard inside ufdr_parser: it records the *shape* of the decoded data —
which model/submodel/sub-submodel types appear, how they relate, and which field names
each carries — in a small SQLite baseline, then warns when a later report drifts from it
(a new/removed model type, a new relation, a new/removed field). This is the field-level
successor to the legacy ``knownModels.db`` last-seen tracker.

See baseline.Baseline (the store) and drift.check_drift (the comparison).
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from ufdr_parser.model_signatures.baseline import Baseline
from ufdr_parser.model_signatures.drift import DriftReport, check_drift

# The drift check is mandatory, so it needs a persistent default baseline. It lives in
# this package directory (clone-and-run friendly) and accumulates the known model shape
# across runs; override the location with the CLI's --baseline. importlib.resources keeps
# this correct regardless of how the package is located.
BASELINE_FILENAME = "baseline.db"


def default_baseline_path() -> Path:
    return Path(str(files("ufdr_parser.model_signatures"))) / BASELINE_FILENAME


__all__ = ["Baseline", "DriftReport", "check_drift", "default_baseline_path"]
