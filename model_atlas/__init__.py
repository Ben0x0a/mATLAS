"""Public package interface for SpatioTemporal Analysis - Forensic."""
from __future__ import annotations

__version__ = "0.1.0"

# __version__ is defined ABOVE this import: app.pipeline reads it back from the
# partially-initialised package (from model_atlas import __version__), which only
# works because the name is already bound when the submodule import starts.
from model_atlas.app.pipeline import ProcessResult, process

__all__ = [
    "ProcessResult",
    "__version__",
    "process",
]
