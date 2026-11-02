"""ufdr_parser — standalone streaming Cellebrite UFDR (report.xml) -> CSV dumper.

A side package, fully decoupled from model_atlas. It streams a Cellebrite UFED Physical
Analyzer report (up to ~32 GB) and dumps every decoded model to one CSV per model type,
joining each record to its on-device source file (path/table/offset) via the report's
trailing ``<extraInfos>`` block. See dump.run_dump for the entry point.
"""
from __future__ import annotations

__all__ = ["DumpSummary", "run_dump"]


def __getattr__(name: str) -> object:
    """Load the dumper on first use. WHY: it needs lxml (the ``ufdr`` extra), while the GUI imports
    ``ufdr_parser.const`` at start-up and must start without that extra."""
    if name in __all__:
        from ufdr_parser import dump

        return getattr(dump, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
