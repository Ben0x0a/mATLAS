"""The post-dump brick contract: what a brick receives, returns, and how bricks are run.

Defines:    DumpOutput (the raw CSV output a brick consumes), BrickResult, the Brick protocol,
            register / registered_bricks (the registry) and run_bricks.
Used by:    dump (runs every registered brick after the CSVs are written), each brick module
            (registers itself), and the bricks package (imports the bricks).
Depends on: standard library only.

WHY bricks: the raw per-model CSVs are the dumper's reference output; everything derived from
them (the combined workbook today, other views later) is a separate capability. A brick reads
ONLY the written CSVs — never the parser's internals — so it can be added, removed or re-run
without touching the parse, and adding one is one new module plus one ``register`` call.

WHY a failing brick does not fail the dump: by the time bricks run, the CSVs (the evidence
output) are complete on disk. A bug in a derived view must not make the operator think the dump
itself failed, so the error is logged with its traceback and reported as a failed BrickResult.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

BRICK_STATUS_OK = "ok"
BRICK_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class DumpOutput:
    """The finished raw output of one dump, as a brick sees it."""

    input_path: Path
    out_dir: Path
    base: str
    # (level, model_type) -> the CSV written for that group.
    group_paths: dict[tuple[int, str], Path]
    # (child_type, top_type) relations observed while parsing.
    relations: frozenset[tuple[str, str]]
    # The report_* traceability values stamped on every CSV row.
    trace: dict[str, str]
    # The report header CSV (case, extraction, images), see ufdr_parser.report_info.
    report_info_path: Path | None = None
    # The bricks' configuration file (e.g. ufdr_config.toml); None = each brick's own lookup.
    config_path: Path | None = None
    # The mATLAS case the dump was run for (names the outputs); None outside a case.
    case_name: str | None = None


@dataclass
class BrickResult:
    name: str
    status: str
    files: list[Path] = field(default_factory=list)  # mutable default — one list per result
    error: str | None = None


class Brick(Protocol):
    """A post-dump step: turns the raw CSV output into a derived artefact."""

    name: str
    description: str

    def run(self, output: DumpOutput) -> list[Path]:
        """Build the artefact(s) under ``output.out_dir``; return the files written."""
        ...


_REGISTRY: dict[str, Brick] = {}


def register(brick: Brick) -> None:
    # A duplicate name would silently replace a brick (and run order would change), so it is
    # treated as a programming error at import time.
    assert brick.name not in _REGISTRY, f"Invariant violated: brick {brick.name!r} registered twice"
    _REGISTRY[brick.name] = brick


def registered_bricks() -> list[Brick]:
    """Every registered brick, in registration order (the order they run in)."""
    return list(_REGISTRY.values())


def validate_bricks(names: set[str] | None, config_path: Path | None) -> None:
    """Let each brick that will run check its configuration up front (optional ``validate``).

    WHY before the dump: a configuration error found only after a long parse wastes the run.
    """
    for brick in registered_bricks():
        if names is not None and brick.name not in names:
            continue
        validate = getattr(brick, "validate", None)
        if validate is not None:
            validate(config_path)


def run_bricks(output: DumpOutput, names: set[str] | None = None) -> list[BrickResult]:
    """Run every registered brick (or only ``names``) over ``output``, in order."""
    results: list[BrickResult] = []
    for brick in registered_bricks():
        if names is not None and brick.name not in names:
            continue
        log.info(f"Running brick '{brick.name}': {brick.description}")
        try:
            files = brick.run(output)
        except Exception as exc:  # noqa: BLE001 - see module docstring: the CSVs stand regardless
            log.exception(f"Brick '{brick.name}' failed; the raw CSVs are unaffected")
            results.append(BrickResult(brick.name, BRICK_STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"))
            continue
        results.append(BrickResult(brick.name, BRICK_STATUS_OK, files=files))
        for path in files:
            log.info(f"Brick '{brick.name}' wrote {path}")
    return results
