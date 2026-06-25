"""RAM-adaptive parallelism for the dump's CSV-writing phase.

Defines:    choose_workers (how many workers to use) and parallel_write_groups (fan the
            per-(level, type) CSV writes out across processes).
Used by:    writer.CsvSink.flush (when workers > 1) and cli (``--workers``).
Depends on: writer (write_group, GroupSpec), source_lookup (resolve_from_conn), psutil
            (optional, for the memory budget).

WHY only the write phase: the parse is a single forward XML stream (already flat-RAM and
CPU-bound in lxml's C core), so it is not split here. The flush, by contrast, is
embarrassingly parallel — each (level, type) CSV is independent and every worker reads the
already-committed spill database read-only — so it parallelises cleanly and safely. Worker
count is bounded by both CPU count and available memory, per operator hardware.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from ufdr_parser.archive import (
    KIND_DEFLATE,
    KIND_PLAIN,
    extract_report,
    open_seekable,
    report_compression,
)
from ufdr_parser.const import LEVEL_FILE_PREFIX, SOURCE_COLUMNS
from ufdr_parser.reader import BlockRangeReader, extra_infos_stream, iter_block_models
from ufdr_parser.scan import index_model_types
from ufdr_parser.source_lookup import SourceIndex, index_source_stream, resolve_from_conn
from ufdr_parser.writer import (
    CsvSink,
    GroupSpec,
    id_columns,
    open_group_writer,
    write_group,
    write_rows,
)

log = logging.getLogger(__name__)

# A flush worker streams one group at a time (rows are not held in RAM), so its footprint
# is small; this conservative per-worker budget still lets a low-memory host scale down.
_PER_WORKER_RAM_MB = 256
_DEFAULT_CAP = 8


def choose_workers(*, cap: int = _DEFAULT_CAP, per_worker_ram_mb: int = _PER_WORKER_RAM_MB) -> int:
    """Pick a worker count from CPU count and available RAM (never below 1).

    CPU count sets the ceiling; available memory can lower it on a constrained host. If
    psutil is unavailable the memory guard is skipped and only the CPU/cap bound applies.
    """
    workers = min(os.cpu_count() or 1, cap)
    try:
        import psutil

        available = psutil.virtual_memory().available
        ram_bound = max(1, int(available / (per_worker_ram_mb * 1024 * 1024)))
        workers = min(workers, ram_bound)
    except Exception:  # noqa: BLE001 - psutil missing or unreadable: fall back to CPU bound
        log.debug("psutil unavailable; sizing workers by CPU count only", exc_info=True)
    return max(1, workers)


def _write_group_worker(spill_path: str, spec: GroupSpec) -> tuple[str, int]:
    """Process-pool task: write one group's CSV from a read-only spill connection."""
    conn = sqlite3.connect(f"file:{spill_path}?mode=ro", uri=True)
    try:
        count = write_group(conn, partial(resolve_from_conn, conn), spec)
    finally:
        conn.close()
    return spec.path.name, count


def parallel_write_groups(
    spill_path: str, specs: list[GroupSpec], workers: int
) -> dict[str, int]:
    """Write every group CSV across ``workers`` processes; return {filename: row_count}."""
    written: dict[str, int] = {}
    log.info(f"Flushing {len(specs)} CSV group(s) across {workers} worker(s)")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for filename, count in pool.map(partial(_write_group_worker, spill_path), specs):
            written[filename] = count
            log.debug(f"Wrote {count} record(s) to {filename}")
    return written


# --------------------------------------------------------------------------- by-type parse

# Default ceiling for parse workers (one whole model type per task; the dominant type bounds
# the wall time, so beyond a few workers adds little).
_PARSE_CAP = 4


@dataclass
class _BlockResult:
    """What one worker produced for its modelType block (paths/metadata, not the rows)."""

    spill_path: str
    # (level, model_type) -> field names in first-seen order, for header union.
    field_order: dict[tuple[int, str], list[str]]
    relations: list[tuple[str, str]]
    types_by_level: dict[int, list[str]]
    record_count: int


@dataclass
class ParallelDumpResult:
    """Aggregated outcome of a by-type parallel dump (mapped to DumpSummary by the caller)."""

    files: dict[str, int] = field(default_factory=dict)
    types_by_level: dict[int, set[str]] = field(default_factory=dict)
    relations: set[tuple[str, str]] = field(default_factory=set)
    fields_by_type: dict[tuple[int, str], set[str]] = field(default_factory=dict)
    record_count: int = 0
    source_count: int = 0


def _spill_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    return conn


def _parse_block_worker(args: tuple) -> _BlockResult:
    """Process-pool task: parse one modelType block (by byte range) into its own spill DB."""
    path, kind, block, spill_path = args
    conn = _spill_connect(spill_path)
    sink = CsvSink(conn)
    types: dict[int, set[str]] = {}
    relations: set[tuple[str, str]] = set()
    count = 0
    with open_seekable(Path(path), kind) as stream:
        stream.seek(block.start)
        reader = BlockRangeReader(stream, block.end - block.start)
        for record in iter_block_models(reader):
            sink.add(record)
            types.setdefault(record.level, set()).add(record.model_type)
            if record.level > 0:
                relations.add((record.model_type, record.top_type))
            count += 1
    sink.persist()
    field_order = {key: list(order) for key, order in sink.field_order().items()}
    conn.close()
    return _BlockResult(
        spill_path=spill_path,
        field_order=field_order,
        relations=list(relations),
        types_by_level={level: list(names) for level, names in types.items()},
        record_count=count,
    )


def _build_source_worker(args: tuple) -> tuple[str, int]:
    """Process-pool task: build the model-id -> source map from the extraInfos region."""
    path, kind, extra_start, source_db = args
    conn = _spill_connect(source_db)
    if extra_start is None:
        SourceIndex(conn).finalise()  # create an empty map
        conn.close()
        return source_db, 0
    with open_seekable(Path(path), kind) as stream:
        stream.seek(extra_start)
        count = index_source_stream(extra_infos_stream(stream), conn)
    conn.close()
    return source_db, count


def run_parse_by_type(
    input_path: Path,
    out_dir: Path,
    base: str,
    *,
    models: set[str] | None,
    workers: int,
) -> ParallelDumpResult:
    """Parse decodedData by model type across ``workers`` processes, then merge to CSVs.

    Each worker owns one ``<modelType>`` block (seeking to its byte range); the trailing
    ``<extraInfos>`` source map is built concurrently. The central merge then writes one CSV
    per (level, type), unioning headers across workers and joining the source map. Output is
    identical to the serial dump (contiguous blocks preserve document order).
    """
    input_path = Path(input_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="ufdr-parallel-"))
    try:
        # A DEFLATE report cannot be seeked cheaply; extract it once so workers seek a copy.
        compression = report_compression(input_path)
        if compression == KIND_DEFLATE:
            log.info("DEFLATE report: extracting report.xml to a temp copy for seeking")
            report_path = extract_report(input_path, temp / "report.xml")
            kind = KIND_PLAIN
        else:
            report_path = input_path
            kind = compression

        with open_seekable(report_path, kind) as stream:
            index = index_model_types(stream)
        blocks = [b for b in index.blocks if models is None or b.model_type in models]
        log.info(f"Parsing {len(blocks)} modelType block(s) across {workers} worker(s)")

        source_db = str(temp / "source.sqlite")
        results: list[_BlockResult] = []
        source_count = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            source_future = pool.submit(
                _build_source_worker,
                (str(report_path), kind, index.extra_info_start, source_db),
            )
            block_futures = [
                pool.submit(
                    _parse_block_worker,
                    (str(report_path), kind, block, str(temp / f"spill_{i}.sqlite")),
                )
                for i, block in enumerate(blocks)
            ]
            _, source_count = source_future.result()
            results = [fut.result() for fut in block_futures]  # block (document) order

        return _merge(results, source_db, out_dir, base, source_count)
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _merge(
    results: list[_BlockResult],
    source_db: str,
    out_dir: Path,
    base: str,
    source_count: int,
) -> ParallelDumpResult:
    """Write one CSV per (level, type), unioning worker headers and joining the source map."""
    # Union field order per group, and record which spills contribute (both in block order).
    field_union: dict[tuple[int, str], dict[str, None]] = {}
    group_spills: dict[tuple[int, str], list[str]] = {}
    for result in results:
        for key, fields in result.field_order.items():
            order = field_union.setdefault(key, {})
            for name in fields:
                order.setdefault(name, None)
            group_spills.setdefault(key, []).append(result.spill_path)

    source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    spill_conns = {
        r.spill_path: sqlite3.connect(f"file:{r.spill_path}?mode=ro", uri=True)
        for r in results
    }
    out = ParallelDumpResult(source_count=source_count)
    try:
        resolve = partial(resolve_from_conn, source_conn)
        for (level, model_type) in sorted(field_union):
            header = (
                id_columns(level)
                + list(field_union[(level, model_type)])
                + list(SOURCE_COLUMNS)
            )
            filename = f"{base}_{LEVEL_FILE_PREFIX[level]}{model_type}.csv"
            handle, csv_writer = open_group_writer(out_dir / filename, header)
            total = 0
            try:
                for spill_path in group_spills[(level, model_type)]:
                    total += write_rows(
                        csv_writer, spill_conns[spill_path], level, model_type, resolve
                    )
            finally:
                handle.close()
            out.files[filename] = total
            log.info(f"Wrote {total} {model_type} record(s) (level {level}) to {filename}")
    finally:
        source_conn.close()
        for conn in spill_conns.values():
            conn.close()

    # Aggregate the observed shape for the run summary + drift check.
    for result in results:
        out.record_count += result.record_count
        for level, names in result.types_by_level.items():
            out.types_by_level.setdefault(level, set()).update(names)
        out.relations.update(result.relations)
    for (level, model_type), order in field_union.items():
        out.fields_by_type[(level, model_type)] = set(order)
    return out
