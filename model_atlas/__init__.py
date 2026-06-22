"""Public package interface for SpatioTemporal Analysis - Forensic."""
from __future__ import annotations

__version__ = "0.1.0"

from model_atlas.pipeline import ProcessResult, process

__all__ = [
    "ProcessResult",
    "__version__",
    "process",
]
