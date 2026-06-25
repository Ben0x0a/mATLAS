"""Command-line entry point for the standalone UFDR -> CSV dumper.

Defines:    build_parser and main (the ``ufdr-parser`` / ``python -m ufdr_parser`` CLI).
Used by:    __main__ and the pyproject console script.
Depends on: dump (run_dump), standard library argparse/logging.

The dumper is decoupled from model_atlas: it only turns a Cellebrite report into per-model
CSVs with source provenance. Diagnostics go to stderr via logging (f-strings); the CSVs
are the deliverable.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from ufdr_parser.dump import run_dump

log = logging.getLogger("ufdr_parser")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ufdr-parser",
        description="Dump every decoded model in a Cellebrite UFDR report to per-model CSVs.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Diagnostic verbosity on stderr (default: INFO).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dump_cmd = sub.add_parser("dump", help="Dump a .ufdr (or bare report .xml) to CSVs.")
    dump_cmd.add_argument("input", type=Path, help="Path to a .ufdr archive or a report.xml.")
    dump_cmd.add_argument(
        "-o", "--output", type=Path, required=True, help="Output directory for the CSVs."
    )
    dump_cmd.add_argument(
        "--models",
        default=None,
        help="Comma-separated top-level model types to dump (default: all).",
    )
    dump_cmd.add_argument(
        "--baseline",
        type=Path,
        default=None,
        metavar="BASELINE.db",
        help="Override the model-drift baseline path. The drift check always runs "
        "(created on first use); this only changes where the baseline is stored.",
    )
    dump_cmd.add_argument(
        "--workers",
        default="1",
        metavar="auto|N",
        help="CSV-writing workers: 'auto' (size to CPU/RAM) or an integer (default: 1).",
    )
    dump_cmd.add_argument(
        "--parse-workers",
        default="1",
        metavar="auto|N",
        help="Parse decodedData by model type across this many processes: 'auto' (size to "
        "CPU/RAM, capped at 4) or an integer (default: 1 = single-stream).",
    )
    return parser


def _resolve_workers(value: str, *, cap: int = 8) -> int:
    """Turn a workers argument ('auto' or an integer) into a worker count."""
    if value == "auto":
        from ufdr_parser.parallel import choose_workers

        return choose_workers(cap=cap)
    try:
        return max(1, int(value))
    except ValueError:
        raise SystemExit(f"worker count must be 'auto' or an integer, got {value!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.command == "dump":
        models = (
            {name.strip() for name in args.models.split(",") if name.strip()}
            if args.models
            else None
        )
        summary = run_dump(
            args.input,
            args.output,
            models=models,
            workers=_resolve_workers(args.workers),
            parse_workers=_resolve_workers(args.parse_workers, cap=4),
        )
        log.info(
            f"Done: {summary.record_count} record(s) across {len(summary.files)} file(s)."
        )
        _report_drift(args.baseline, summary)
        return 0
    return 2


def _report_drift(baseline_path: "Path | None", summary) -> None:
    """Run the mandatory model-signatures drift check and log the outcome."""
    from ufdr_parser.model_signatures import check_drift, default_baseline_path

    baseline_path = baseline_path or default_baseline_path()
    report = check_drift(summary, baseline_path)
    if report.baseline_established:
        log.info(f"Model-signature baseline established at {baseline_path}")
    elif report.has_shifts:
        log.warning(f"Model drift vs {baseline_path}: {len(report.warnings())} shift(s)")
        for line in report.warnings():
            log.warning(f"  drift: {line}")
    else:
        log.info(f"No model drift vs {baseline_path}")


if __name__ == "__main__":
    raise SystemExit(main())
