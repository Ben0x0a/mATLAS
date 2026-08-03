"""Thin argparse launcher for the declarative spatio-temporal pipeline."""
from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

from model_atlas import __version__
from model_atlas.app.config import get_settings
from model_atlas.app.diagnostics import install_excepthook
from model_atlas.app.logging_setup import configure_logging
from model_atlas.app.pipeline import process
from model_atlas.export.registry import available_exporters, load_builtin_exporters
from model_atlas.app.profiles import build_profile_preset_folder, load_profile

log = logging.getLogger("model_atlas.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matlas",
        description=(
            "SpatioTemporal Analysis - Forensic Data Processing. Process CSV, Excel, "
            "SQLite and XML sources into the relational spatio-temporal assertion "
            "model (flat CSV projection by default)."
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
        default=None,
        help="Logging verbosity (overrides matlas_config.toml [logging].level; default INFO). "
             "Use DEBUG for a precise step-by-step trace.",
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
            "account the data came from. A literal is used verbatim; a preset-style "
            "reference such as field(Column name) resolves per source row."
        ),
    )
    process_parser.add_argument(
        "--concerns",
        required=True,
        help=(
            "Entity linked to every output row. When set it OVERRIDES any concerns "
            "a preset maps (the preset value is the default otherwise), e.g. a person, "
            "company, account, or case subject. A literal is used verbatim; a preset-style "
            "reference such as field(Evidence number) resolves per source row, so one input "
            "carrying several subjects maps each row to its own concerns value."
        ),
    )
    process_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "Output folder. The case database is written inside as <name>.matlas.sqlite "
            "(created, or APPENDED to if it already exists); every other export is one "
            "file per format beside it (<output>/<name>.<ext>)."
        ),
    )
    process_parser.add_argument(
        "--name",
        default="merged",
        help=(
            "Stem for the case database and every output file: the case DB becomes "
            "<output>/<name>.matlas.sqlite and each export/sidecar shares the stem. "
            "Default 'merged'."
        ),
    )
    process_parser.add_argument(
        "--case",
        default=None,
        help=(
            "Optional human case label (e.g. 'Operation Falcon'). Stored on the case when "
            "it is CREATED; falls back to --name. On APPEND the label is read from the "
            "existing case database and this value is ignored."
        ),
    )
    process_parser.add_argument(
        "--root-prefix-depth",
        type=int,
        default=None,
        help=(
            "How many leading path segments a selector 'path' may skip so /private/... "
            "matches filesystem1/private/... (the acquisition's root wrapper). Unset falls "
            "back to matlas_config.toml [discovery].root_prefix_depth (default 1), matching "
            "the GUI — so the same config yields the same selectors on both frontends."
        ),
    )
    process_parser.add_argument(
        "--traceability-format",
        choices=("readable", "prov"),
        default="readable",
        help="Traceability sidecar format: human-readable (default) or W3C PROV-JSON.",
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
    load_builtin_exporters()
    builtin_names = ", ".join(spec.name for spec in available_exporters())
    process_parser.add_argument(
        "--export",
        action="append",
        metavar="NAME",
        default=None,
        help=(
            "Write an additional export alongside the CSV; repeatable. "
            f"Available: {builtin_names}."
        ),
    )

    return parser


def _configure_logging(log_file: Path | None, log_level: str | None) -> None:
    # CLI --log-level wins; otherwise the config default (then INFO). Format from config.
    settings = get_settings()
    configure_logging(log_level or settings.logging.level, log_file, settings.logging.format)


def _cmd_process(args: argparse.Namespace) -> int:
    # Build the shared process() keyword arguments ONCE so the profile and non-profile
    # branches can never drift (a past bug silently dropped --root-prefix-depth under --profile).
    kwargs = dict(
        base_name=args.name,
        case=args.case,
        traceability_format=args.traceability_format,
        entity=args.entity,
        concerns=args.concerns,
        include_source_columns=args.include_source_columns,
        root_prefix_depth=args.root_prefix_depth,
        exports=args.export,
    )
    if args.profile is None:
        result = process(args.input, args.presets, args.output, **kwargs)
    else:
        with tempfile.TemporaryDirectory(prefix="matlas-profile-") as tmp:
            profile_presets = load_profile(args.profile)
            presets_path = build_profile_preset_folder(profile_presets, Path(tmp))
            result = process(args.input, presets_path, args.output, **kwargs)
    log.info(f"Row counts: {result.row_counts}")
    for entry in result.matched:
        log.info(f"Matched: {entry}")
    log.info(f"Unmatched sources: {len(result.unmatched)}")
    for warning in result.warnings[:20]:
        log.warning(f"{warning}")
    log.info(f"Transform warnings: {len(result.warnings)}")
    # The case database is always written (the run itself, created or appended).
    log.info(f"Case database: {result.output_case_db}")
    if result.output_csv is not None:
        log.info(f"CSV: {result.output_csv}")
        log.info(f"Traceability: {result.output_traceability}")
        log.info(f"Warnings report: {result.output_warnings}")
    elif result.output_extras:
        # The csv export was deselected but other exports were written — not an empty run.
        log.info(f"No CSV selected; wrote {len(result.output_extras)} export(s).")
    else:
        log.warning("No rows matched a preset; the case database records the run.")
    for extra in result.output_extras:
        log.info(f"Export: {extra}")
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_file, args.log_level)
    # Install the crash handler once logging is live so a report path can be logged, and
    # before process() runs so any unhandled error produces a forensic crash artefact.
    install_excepthook(context={"entrypoint": "cli", "command": args.command})
    if args.command == "process":
        return _cmd_process(args)
    parser.print_help()
    return 1
