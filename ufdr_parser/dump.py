"""Orchestrate a single-pass UFDR report dump to per-model CSVs.

Defines:    DumpSummary (run result) and run_dump (open -> stream -> spill -> flush).
Used by:    cli, gui.app and tests.
Depends on: archive, reader, models, source_lookup, writer, report_info, sqlite3; bricks
            (imported lazily, run after the CSVs are written).

The input is a .ufdr archive (report.xml streamed from inside it) or a bare report XML such
as a Physical Analyzer "XML report" export; ``archive`` tells them apart by header. One forward
pass over the report feeds two sinks sharing one temporary SQLite database: decoded models are
buffered by CsvSink; the trailing <extraInfos> id->source map is built in SourceIndex. At the
end the buffered rows are flushed to CSV with each row joined to its source and stamped with
the report file it came from (``report_file`` / ``report_entry``). Holding nothing per-record
in RAM keeps the run flat for 32 GB reports. Once the CSVs are complete, the registered bricks
(e.g. the combined workbook) build their derived artefacts from them — see ``bricks``.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ufdr_parser.archive import open_report, report_size, report_trace
from ufdr_parser.const import FILENAME_FORBIDDEN_CHARACTERS
from ufdr_parser.models import FlatRecord
from ufdr_parser.reader import iter_items
from ufdr_parser.report_info import write_report_info
from ufdr_parser.source_lookup import EXTRA_INFO_MODEL_KIND, SourceEntry, SourceIndex
from ufdr_parser.writer import CsvSink, group_filenames

log = logging.getLogger(__name__)

# A progress line is logged at most this often during the parse (and checked every
# _PROGRESS_CHECK_EVERY items, so the clock is not read per record). WHY: the parse of a large
# report is the longest phase and was silent from its first log line to the CSV writes, so a
# slow run was indistinguishable from a frozen one; a periodic line shows it is moving and how
# far (bytes read vs the report size).
_PROGRESS_INTERVAL_S = 10.0
_PROGRESS_CHECK_EVERY = 2000
_MB = 1024 * 1024


class _CountingReader:
    """A read()-only pass-through that counts the bytes read from the report stream."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data


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
    # Number of MODEL source entries in <extraInfos> (file node entries are not counted).
    source_count: int = 0
    # One BrickResult per post-dump brick run. mutable default — one list per summary
    bricks: list = field(default_factory=list)


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
    bricks: set[str] | None = None,
    brick_config: Path | None = None,
    case_name: str | None = None,
) -> DumpSummary:
    """Dump every (or a ``models``-filtered subset of) model type to CSV under ``out_dir``.

    After the CSVs are written, the post-dump bricks run on them: every registered brick by
    default, only those named in ``bricks`` otherwise (an empty set runs none).

    ``models`` filters by top-level model type: a Location filter keeps Location rows and
    its child rows (Coordinate/StreetAddress) but not standalone Coordinate models.
    ``workers`` > 1 fans the CSV-writing phase out across processes; ``parse_workers`` > 1
    parses decodedData by model type across processes (a separate, heavier parallelism).
    ``brick_config`` is the bricks' configuration file (the workbook's ufdr_config.toml); None
    lets each brick find its own (./ufdr_config.toml, else its built-in default). It is checked
    BEFORE parsing, so a configuration typo fails in seconds rather than after the dump.
    ``case_name`` (the GUI's case) names every output file ``<case>_...`` instead of after the
    input file, and is recorded on the workbook's About sheet; each CSV row still names the
    report it came from (report_file).
    """
    if bricks is None or bricks:
        # Lazy: bricks pull in openpyxl, which a CSV-only caller never needs.
        from ufdr_parser.bricks import validate_bricks

        validate_bricks(bricks, brick_config)
    input_path = Path(input_path)
    out_dir = Path(out_dir)
    if case_name is not None and (not case_name.strip() or set(case_name) & set(FILENAME_FORBIDDEN_CHARACTERS)):
        # The name becomes a filename prefix: a separator would write outside out_dir.
        raise ValueError(f"case name {case_name!r} cannot be empty or contain any of {FILENAME_FORBIDDEN_CHARACTERS}")
    base = case_name.strip() if case_name is not None else input_path.stem
    # Computed from the ORIGINAL input: the parallel path may read a temp extracted copy.
    trace = report_trace(input_path)
    # The report header (case, extraction, images) is not a model: dumped by its own short pass.
    out_dir.mkdir(parents=True, exist_ok=True)
    report_info_path, report_info_rows = write_report_info(input_path, out_dir, base, trace)

    if parse_workers > 1:
        # By-type parallel parse: each worker owns a modelType block, then a central merge.
        from ufdr_parser.parallel import run_parse_by_type

        result = run_parse_by_type(
            input_path, out_dir, base, models=models, workers=parse_workers, trace=trace
        )
        summary = DumpSummary(
            files=result.files,
            types_by_level=result.types_by_level,
            relations=result.relations,
            fields_by_type=result.fields_by_type,
            record_count=result.record_count,
            source_count=result.source_count,
        )
    else:
        summary = _run_serial(input_path, out_dir, base, trace, models=models, workers=workers)

    if summary.record_count and not summary.source_count:
        # WHY warn: blank source_* columns are otherwise indistinguishable from a parser
        # fault. Some exports (e.g. a PA XML report with NodeCount="0") ship an empty
        # <extraInfos>, so the on-device source is simply not in the report.
        log.warning(
            f"{input_path}: the report's <extraInfos> holds no model source entries; "
            "the source_* columns are blank for every row"
        )
    summary.files[report_info_path.name] = report_info_rows
    log.info(
        f"Dump complete: {summary.record_count} record(s), "
        f"{summary.source_count} source entr(ies), {len(summary.files)} CSV file(s)"
    )
    if bricks is None or bricks:
        summary.bricks = _run_bricks(
            input_path, out_dir, base, trace, summary, bricks, report_info_path, brick_config, case_name
        )
    return summary


def _run_bricks(
    input_path: Path,
    out_dir: Path,
    base: str,
    trace: dict[str, str],
    summary: DumpSummary,
    names: set[str] | None,
    report_info_path: Path,
    config_path: Path | None,
    case_name: str | None,
) -> list:
    """Hand the finished CSV output to the post-dump bricks."""
    # Lazy: bricks pull in openpyxl, which a CSV-only caller never needs.
    from ufdr_parser.bricks import DumpOutput, run_bricks

    # The group -> file mapping is recomputed by the SAME function the CSV writers used (one
    # source of truth), from the groups the parse observed.
    filenames = group_filenames(summary.fields_by_type.keys(), base)
    output = DumpOutput(
        input_path=input_path,
        out_dir=out_dir,
        base=base,
        group_paths={key: out_dir / name for key, name in filenames.items()},
        relations=frozenset(summary.relations),
        trace=trace,
        report_info_path=report_info_path,
        config_path=config_path,
        case_name=case_name,
    )
    return run_bricks(output, names)


def _run_serial(
    input_path: Path,
    out_dir: Path,
    base: str,
    trace: dict[str, str],
    *,
    models: set[str] | None,
    workers: int,
) -> DumpSummary:
    """The single-stream parse: one pass feeding CsvSink + SourceIndex, then the flush."""
    summary = DumpSummary()

    temp_dir = Path(tempfile.mkdtemp(prefix="ufdr-parser-"))
    conn = _new_spill_db(temp_dir)
    try:
        sink = CsvSink(conn)
        source = SourceIndex(conn)
        total = report_size(input_path)
        started = last_logged = time.monotonic()
        with open_report(input_path) as raw_stream:
            stream = _CountingReader(raw_stream)
            for count, item in enumerate(iter_items(stream), start=1):
                if isinstance(item, FlatRecord):
                    if models is None or item.top_type in models:
                        sink.add(item)
                        _track(summary, item)
                        summary.record_count += 1
                elif isinstance(item, SourceEntry):
                    source.add(item)
                    if item.kind == EXTRA_INFO_MODEL_KIND:
                        summary.source_count += 1
                if count % _PROGRESS_CHECK_EVERY == 0 and time.monotonic() - last_logged >= _PROGRESS_INTERVAL_S:
                    last_logged = time.monotonic()
                    log.info(
                        f"Parsing: {summary.record_count} record(s), {summary.source_count} source "
                        f"entr(ies); {stream.bytes_read / _MB:,.0f} of {total / _MB:,.0f} MB read"
                    )
        log.info(
            f"Parsed {summary.record_count} record(s) and {summary.source_count} source entr(ies) "
            f"in {time.monotonic() - started:,.0f} s"
        )
        source.finalise()
        log.info("Writing the CSV files")
        summary.files = sink.flush(out_dir, base, source, trace, workers=workers)
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
