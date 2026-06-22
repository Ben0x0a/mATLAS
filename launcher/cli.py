"""Thin argparse launcher for the declarative spatio-temporal pipeline."""
from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from model_atlas import __version__
from model_atlas.pipeline import process
from launcher.profiles import build_profile_preset_folder, load_profile

log = logging.getLogger("model_atlas.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matlas",
        description=(
            "SpatioTemporal Analysis - Forensic Data Processing. Process CSV, Excel and "
            "SQLite sources into a merged 43-column spatio-temporal assertion model."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append run logs to this file in addition to stderr.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Logging verbosity. Use DEBUG for a precise step-by-step trace.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    process_parser = sub.add_parser(
        "process",
        help="Process CSV, Excel and SQLite sources into the spatio-temporal model.",
    )
    process_parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Input file or folder containing CSV, Excel, or SQLite sources.",
    )
    process_parser.add_argument(
        "--presets",
        type=Path,
        default=Path("presets"),
        help="Declarative preset YAML file or folder. Ignored when --profile is supplied.",
    )
    process_parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="Load preset YAML paths from a .mATLAS-profile file.",
    )
    process_parser.add_argument(
        "--entity",
        default=None,
        help=(
            "Entity for every output row. When set it OVERRIDES any entity a preset "
            "maps (the preset value is the default otherwise), e.g. the device or "
            "account the data came from."
        ),
    )
    process_parser.add_argument(
        "--linked-entity",
        required=True,
        help=(
            "Entity linked to every output row. When set it OVERRIDES any linked_entity "
            "a preset maps (the preset value is the default otherwise), e.g. a person, "
            "company, account, or case subject."
        ),
    )
    process_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "Output path. Merge mode (default): path for the merged CSV. "
            "Split mode (--no-merge): folder that receives one CSV per matched preset."
        ),
    )
    process_parser.add_argument(
        "--root-prefix-depth",
        type=int,
        default=1,
        help=(
            "How many leading path segments a selector 'path' may skip so /private/... "
            "matches filesystem1/private/... (the acquisition's root wrapper). Default 1."
        ),
    )
    process_parser.add_argument(
        "--traceability-format",
        choices=("readable", "prov"),
        default="readable",
        help="Traceability sidecar format: human-readable (default) or W3C PROV-JSON.",
    )
    process_parser.add_argument(
        "--merge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Merge all matched sources into one CSV (default). "
            "Use --no-merge to write one CSV per matched preset into the output folder."
        ),
    )
    process_parser.add_argument(
        "--source-columns",
        action=argparse.BooleanOptionalAction,
        default=True,
        dest="include_source_columns",
        help=(
            "Append every source column verbatim as an orig_<col> column after the "
            "canonical schema (default). Use --no-source-columns for canonical columns only."
        ),
    )

    return parser


def _configure_logging(log_file: Path | None, log_level: str) -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    root_logger.setLevel(level)
    for handler in handlers:
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)


def _cmd_process(args: argparse.Namespace) -> int:
    if args.profile is None:
        result = process(
            args.input, args.presets, args.output,
            traceability_format=args.traceability_format,
            merge=args.merge,
            entity=args.entity,
            linked_entity=args.linked_entity,
            include_source_columns=args.include_source_columns,
            root_prefix_depth=args.root_prefix_depth,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="matlas-profile-") as tmp:
            profile_presets = load_profile(args.profile)
            presets_path = build_profile_preset_folder(profile_presets, Path(tmp))
            result = process(
                args.input, presets_path, args.output,
                traceability_format=args.traceability_format,
                merge=args.merge,
                entity=args.entity,
                linked_entity=args.linked_entity,
                include_source_columns=args.include_source_columns,
            )
    log.info(f"Row counts: {result.row_counts}")
    for entry in result.matched:
        log.info(f"Matched: {entry}")
    log.info(f"Unmatched sources: {len(result.unmatched)}")
    for warning in result.warnings[:20]:
        log.warning(f"{warning}")
    log.info(f"Transform warnings: {len(result.warnings)}")
    if result.output_csv is not None:
        log.info(f"CSV: {result.output_csv}")
        log.info(f"Traceability: {result.output_traceability}")
        log.info(f"Warnings report: {result.output_warnings}")
    elif result.output_csvs:
        for csv in result.output_csvs:
            log.info(f"CSV: {csv}")
    else:
        log.warning("No rows produced: no discovered source matched a preset.")
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    if args.command == "process":
        return _cmd_process(args)
    parser.print_help()
    return 1
