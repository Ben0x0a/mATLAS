"""ufdr_parser — standalone streaming Cellebrite UFDR (report.xml) -> CSV dumper.

A side package, fully decoupled from model_atlas. It streams a Cellebrite UFED Physical
Analyzer report (up to ~32 GB) and dumps every decoded model to one CSV per model type,
joining each record to its on-device source file (path/table/offset) via the report's
trailing ``<extraInfos>`` block. See dump.run_dump for the entry point.
"""
from __future__ import annotations

from ufdr_parser.dump import DumpSummary, run_dump

__all__ = ["DumpSummary", "run_dump"]
