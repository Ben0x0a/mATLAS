"""Orchestrate a single-pass UFDR report dump to per-model CSVs.

Defines:    DumpSummary (run result) and run_dump (open -> stream -> spill -> flush).
Used by:    cli and tests.
Depends on: archive, reader, models, source_lookup, writer, sqlite3.

One forward pass over report.xml feeds two sinks sharing one temporary SQLite database:
decoded models are buffered by CsvSink; the trailing <extraInfos> id->source map is built
in SourceIndex. At the end the buffered rows are flushed to CSV with each row joined to
its source. Holding nothing per-record in RAM keeps the run flat for 32 GB reports.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ufdr_parser.archive import open_report
from ufdr_parser.models import FlatRecord
from ufdr_parser.reader import iter_items
from ufdr_parser.source_lookup import SourceEntry, SourceIndex
from ufdr_parser.writer import CsvSink

log = logging.getLogger(__name__)


@dataclass
class DumpSummary:
    """What a dump produced: output files and the observed model shape."""

    files: dict[str, int] = field(default_factory=dict)
    # level -> set of model types seen at that level.
    types_by_level: dict[int, set[str]] = field(default_factory=dict)
    # (child_type, top_level_type) relations, mirroring the legacy relation tracking.
    relations: set[tuple[str, str]] = field(default_factory=set)
    # (level, model_type) -> set of field names, for the model_signatures drift check.
    fields_by_type: dict[tuple[int, str], set[str]] = field(default_factory=dict)
    record_count: int = 0
    source_count: int = 0


def _new_spill_db(directory: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(directory / "spill.sqlite")
    # Throwaway DB: drop durability for speed; it is deleted at the end of the run.
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    return conn


def run_dump(
    input_path: Path,
    out_dir: Path,
    *,
    models: set[str] | None = None,
    workers: int = 1,
    parse_workers: int = 1,
) -> DumpSummary:
    """Dump every (or a ``models``-filtered subset of) model type to CSV under ``out_dir``.

    ``models`` filters by top-level model type: a Location filter keeps Location rows and
    its child rows (Coordinate/StreetAddress) but not standalone Coordinate models.
    ``workers`` > 1 fans the CSV-writing phase out across processes; ``parse_workers`` > 1
    parses decodedData by model type across processes (a separate, heavier parallelism).
    """
    input_path = Path(input_path)
    out_dir = Path(out_dir)
    base = input_path.stem

    if parse_workers > 1:
        # By-type parallel parse: each worker owns a modelType block, then a central merge.
        from ufdr_parser.parallel import run_parse_by_type

        result = run_parse_by_type(
            input_path, out_dir, base, models=models, workers=parse_workers
        )
        return DumpSummary(
            files=result.files,
            types_by_level=result.types_by_level,
            relations=result.relations,
            fields_by_type=result.fields_by_type,
            record_count=result.record_count,
            source_count=result.source_count,
        )

    summary = DumpSummary()

    temp_dir = Path(tempfile.mkdtemp(prefix="ufdr-parser-"))
    conn = _new_spill_db(temp_dir)
    try:
        sink = CsvSink(conn)
        source = SourceIndex(conn)
        with open_report(input_path) as stream:
            for item in iter_items(stream):
                if isinstance(item, FlatRecord):
                    if models is not None and item.top_type not in models:
                        continue
                    sink.add(item)
                    _track(summary, item)
                    summary.record_count += 1
                elif isinstance(item, SourceEntry):
                    source.add(item)
                    summary.source_count += 1
        source.finalise()
        summary.files = sink.flush(out_dir, base, source, workers=workers)
        log.info(
            f"Dump complete: {summary.record_count} record(s), "
            f"{summary.source_count} source entr(ies), {len(summary.files)} CSV file(s)"
        )
        return summary
    finally:
        conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _track(summary: DumpSummary, record: FlatRecord) -> None:
    """Accumulate the observed model shape for reporting and the drift check."""
    summary.types_by_level.setdefault(record.level, set()).add(record.model_type)
    key = (record.level, record.model_type)
    summary.fields_by_type.setdefault(key, set()).update(record.fields)
    if record.level > 0:
        summary.relations.add((record.model_type, record.top_type))
