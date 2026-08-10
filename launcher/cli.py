"""Thin argparse launcher for the declarative spatio-temporal pipeline.

Defines:    ``build_parser`` / ``run`` (the ``matlas`` entry point) plus the ``process``
            and ``concomitance`` subcommands. ``concomitance`` builds the SAME
            ``ConcomitanceRequest`` and calls the SAME ``run_concomitance`` as the GUI
            panel (``gui.spatiotemporal_controller``), so the two shells can never drift
            on what a request means or what the engine can currently do.
Used by:    the ``matlas`` console-script entry point.
Depends on: ``model_atlas.app.pipeline`` (``process``), ``model_atlas.app.concomitance_run``
            (``run_concomitance``),
            ``model_atlas.analyze.concomitance_request`` (the request dataclasses and
            ``check_request`` — the one validator both shells call),
            ``model_atlas.analyze.case_vocabulary`` (``--list-entities``), and
            ``model_atlas.analyze.concomitance_request`` for ``DURATION_UNIT_NAMES``/``duration_from`` —
            the Qt-free duration-unit table, imported (not copied) so the CLI's ``--before``/
            ``--after`` suffix parsing and the GUI's unit combo can never disagree on what
            a unit means.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import sqlite3
import tempfile
from pathlib import Path

from model_atlas import __version__
from model_atlas.analyze.case_vocabulary import read_case_vocabulary
from model_atlas.analyze.concomitance_request import (
    ConcomitanceRequest,
    Criterion,
    CriterionKind,
    EntityGroup,
    MatchMode,
    Severity,
    check_request,
)
from model_atlas.app.concomitance_run import run_concomitance
from model_atlas.app.config import get_settings
from model_atlas.app.diagnostics import install_excepthook
from model_atlas.app.logging_setup import configure_logging
from model_atlas.app.pipeline import process
from model_atlas.export.registry import available_exporters, load_builtin_exporters
from model_atlas.app.profiles import build_profile_preset_folder, load_profile
from model_atlas.analyze.concomitance_request import DURATION_UNIT_NAMES, duration_from

log = logging.getLogger("model_atlas.cli")

# Suffix letter -> the unit name DURATION_UNIT_NAMES/duration_from already know. Only the
# LETTER mapping lives here; the actual unit->timedelta table stays the single copy in
# gui.spatiotemporal_query, so the CLI cannot silently drift from the GUI combo.
_DURATION_SUFFIXES: dict[str, str] = {
    "s": "seconds", "m": "minutes", "h": "hours", "d": "days",
}
_DURATION_ARG_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<suffix>[a-zA-Z]+)")


def _parse_duration_arg(text: str) -> dt.timedelta:
    """Parse a CLI duration token (``30s`` / ``10m`` / ``2h`` / ``1d``) into a timedelta.

    HOW: split the leading number from the trailing unit letter with a regex, map the
    letter to the unit name via ``_DURATION_SUFFIXES``, then hand off to the GUI's own
    ``duration_from`` for the actual multiplication — so the value*unit arithmetic is
    never duplicated. WHY a regex and not argparse's own float/choices machinery: the
    unit is glued onto the number with no separator ("30s"), which neither type alone
    can parse.
    """
    match = _DURATION_ARG_RE.fullmatch(text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            f"invalid duration {text!r}; expected a number followed by one of "
            f"{sorted(_DURATION_SUFFIXES)} (e.g. 30s, 10m, 2h, 1d)")
    unit = _DURATION_SUFFIXES.get(match.group("suffix").lower())
    if unit is None:
        raise argparse.ArgumentTypeError(
            f"unknown duration unit in {text!r}; allowed suffixes: "
            f"{sorted(_DURATION_SUFFIXES)}")
    try:
        return duration_from(float(match.group("value")), unit)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_criterion_arg(text: str) -> Criterion:
    """Parse one ``--group1``/``--group2`` token (``KIND=VALUE``) into a ``Criterion``.

    An unknown kind or a missing ``=`` raises ``ArgumentTypeError`` — argparse turns that
    into a usage error on stderr (exit 2), never a traceback — and names the allowed
    ``CriterionKind`` values so the operator can fix it without reading the source.
    """
    allowed = ", ".join(kind.value for kind in CriterionKind)
    if "=" not in text:
        raise argparse.ArgumentTypeError(
            f"malformed criterion {text!r}; expected KIND=VALUE with KIND one of: {allowed}")
    kind_text, _, value = text.partition("=")
    try:
        kind = CriterionKind(kind_text.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"unknown criterion kind {kind_text!r}; allowed: {allowed}") from exc
    if not value:
        raise argparse.ArgumentTypeError(f"malformed criterion {text!r}; missing a value")
    return Criterion(kind=kind, value=value)


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

    concomitance_parser = sub.add_parser(
        "concomitance",
        help="Find pairs of placements near each other in space and time.",
    )
    concomitance_parser.add_argument(
        "--case", type=Path, required=True,
        help="The case database (*.matlas.sqlite) both groups are drawn from by default.",
    )
    concomitance_parser.add_argument(
        "--case-2", type=Path, default=None, dest="case_2",
        help="A second case database for group 2 (unsupported until the T102 engine).",
    )
    concomitance_parser.add_argument(
        "--group1", action="append", default=[], type=_parse_criterion_arg,
        metavar="KIND=VALUE", dest="group1",
        help=(
            "One selection criterion for group 1, KIND one of "
            f"{', '.join(k.value for k in CriterionKind)}; repeatable. "
            "Omitted entirely = every entity in the case."
        ),
    )
    concomitance_parser.add_argument(
        "--group1-match", choices=("any", "all"), default="any", dest="group1_match",
        help="How the --group1 criteria combine (default any).",
    )
    concomitance_parser.add_argument(
        "--group2", action="append", default=[], type=_parse_criterion_arg,
        metavar="KIND=VALUE", dest="group2",
        help="One selection criterion for group 2; repeatable. See --group1.",
    )
    concomitance_parser.add_argument(
        "--group2-match", choices=("any", "all"), default="any", dest="group2_match",
        help="How the --group2 criteria combine (default any).",
    )
    concomitance_parser.add_argument(
        "--distance", type=float, default=None,
        help="Distance threshold in metres. Required unless --list-entities is used.",
    )
    concomitance_parser.add_argument(
        "--before", type=_parse_duration_arg, default=None,
        help="How much earlier group 2 may be than group 1, e.g. 30s/10m/2h/1d.",
    )
    concomitance_parser.add_argument(
        "--after", type=_parse_duration_arg, default=None,
        help="How much later group 2 may be than group 1, e.g. 30s/10m/2h/1d.",
    )
    concomitance_parser.add_argument(
        "--accuracy", action="store_true", dest="use_accuracy",
        help="Account for recorded position accuracy (unsupported until the T102 engine).",
    )
    concomitance_parser.add_argument(
        "--output", type=Path, default=None,
        help="CSV path to write results to. Required unless --list-entities is used.",
    )
    concomitance_parser.add_argument(
        "--list-entities", choices=[kind.value for kind in CriterionKind], default=None,
        dest="list_entities", metavar="KIND",
        help=(
            "Print the case's vocabulary for KIND as TSV (value, qualifier, "
            "placement_count) on stdout and exit, without running a query."
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


def _list_entities(case: Path, kind_value: str) -> int:
    """``--list-entities KIND``: TSV (value, qualifier, placement_count) on stdout, exit 0.

    ``print()`` is the documented exception to the logging rule — this is the command's
    result output, not a diagnostic, and must survive a pipe into ``cut``/``grep`` without
    a level/timestamp prefix corrupting it. A case that cannot be opened read-only (missing
    file, wrong file type) is reported through logging and exits 1, never a traceback.
    """
    kind = CriterionKind(kind_value)
    try:
        vocabulary = read_case_vocabulary(case)
    except sqlite3.Error as exc:
        log.error(f"Cannot read case {case}: {exc}")
        return 1
    print("value\tqualifier\tplacement_count")
    for entry in vocabulary[kind]:
        print(f"{entry.value}\t{entry.qualifier or ''}\t{entry.placement_count}")
    return 0


def _cmd_concomitance(args: argparse.Namespace) -> int:
    if args.list_entities is not None:
        return _list_entities(args.case, args.list_entities)

    # --distance/--before/--after/--output are only required for an actual run, not for
    # --list-entities, so they are declared optional at the argparse level and checked here.
    required = {"--distance": args.distance, "--before": args.before,
                "--after": args.after, "--output": args.output}
    missing = [name for name, value in required.items() if value is None]
    if missing:
        log.error(f"Missing required argument(s): {', '.join(missing)}.")
        return 1

    request = ConcomitanceRequest(
        case_1=args.case,
        case_2=args.case_2,
        group_1=EntityGroup(criteria=tuple(args.group1), mode=MatchMode(args.group1_match)),
        group_2=EntityGroup(criteria=tuple(args.group2), mode=MatchMode(args.group2_match)),
        space_threshold_m=args.distance,
        time_before=args.before,
        time_after=args.after,
        use_accuracy=args.use_accuracy,
        output_path=args.output,
    )

    # check_request is the ONE validator both shells call; an ERROR blocks the run here
    # exactly as the GUI blocks Run, so the two shells can never disagree on what is legal.
    problems = check_request(request)
    for problem in problems:
        (log.error if problem.severity is Severity.ERROR else log.warning)(problem.message)
    if any(problem.severity is Severity.ERROR for problem in problems):
        return 1

    try:
        result = run_concomitance(request)
    except ValueError as exc:
        # run_concomitance re-checks request validity itself (no caller can bypass
        # check_request); this mirrors the ERROR path above in case that re-check ever
        # disagrees with the pre-run check performed here.
        log.error(str(exc))
        return 1

    log.info(
        f"{result.match_count} match(es) written to {result.output_path} "
        f"(candidates: group 1 = {result.candidate_count_1}, "
        f"group 2 = {result.candidate_count_2})")
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
    if args.command == "concomitance":
        return _cmd_concomitance(args)
    parser.print_help()
    return 1
