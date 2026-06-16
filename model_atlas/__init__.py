"""Public package interface for SpatioTemporal Analysis - Forensic."""
from __future__ import annotations

__version__ = "0.1.0"

from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.pipeline import ProcessResult, process

__all__ = [
    "DiscoveredElement",
    "ElementType",
    "ProcessResult",
    "__version__",
    "process",
]
