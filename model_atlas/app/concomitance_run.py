"""The concomitance app-layer orchestrator — the ONLY thing either shell calls to run one.

Defines:    ``ConcomitanceRunResult`` (the stable result shape) and ``run_concomitance(
            request, *, on_progress=None, should_cancel=None) -> ConcomitanceRunResult`` —
            owns validate -> open store(s) -> run the engine -> stream to the sink ->
            close everything -> return a summary.
Used by:    ``gui.spatiotemporal_controller`` (through ``TaskRunner``/``ProgressBridge``)
            and ``launcher.cli``'s concomitance subcommand. Neither callback
            parameter is Qt: both are plain callables, so this module stays importable
            with no GUI toolkit installed.
Depends on: ``model_atlas.analyze.concomitance_request`` (``ConcomitanceRequest`` /
            ``check_request`` / ``Problem`` / ``Severity``), ``model_atlas.analyze.
            concomitance_output`` (``CsvMatchSink``), ``model_atlas.analyze.concomitance``
            (``find_concomitant`` — the T102 engine, which now streams ``MatchRow``
            straight into the sink), and the standard library only (``sqlite3``,
            ``dataclasses``, ``pathlib``, ``typing``).

The engine seam (read this before changing anything here)
-----------------------------------------------------------
The four capability guards this layer once carried — two different entity groups, an
asymmetric time window, accuracy-aware matching and a second case — are GONE: the T102
engine honours all four, so this layer now just opens the store(s) and forwards the
request. The engine emits fully-formed ``MatchRow``s itself (it is the only
side that holds both endpoints' position, interval and accuracy), so this module no
longer re-queries either side to enrich a pair.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from model_atlas.analyze.concomitance import find_concomitant
from model_atlas.analyze.concomitance_output import CsvMatchSink
from model_atlas.analyze.concomitance_request import (
    ConcomitanceRequest,
    Problem,
    Severity,
    check_request,
)


@dataclass(frozen=True)
class ConcomitanceRunResult:
    """The stable summary a shell reports after a run."""

    match_count: int
    candidate_count_1: int
    candidate_count_2: int
    output_path: Path
    warnings: tuple[Problem, ...]


# --- read-only store access (the same URI-safety pattern used throughout the GUI) -----

def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only via a percent-encoded ``file:`` URI (mode=ro).

    Copied from ``gui.spatiotemporal_controller._PersistedRunView`` /
    ``model_atlas.analyze.case_vocabulary._open_readonly`` rather than shared across the
    package (each of the three call sites is a small, self-contained helper) — WHY
    ``Path.as_uri()``: it percent-encodes the folder BEFORE ``?mode=ro`` is appended, so a
    folder carrying a URI-significant character ('#', '?', '%') is never mis-parsed into a
    bogus path + fragment. WHY read-only: a case is evidence — a query must never mutate it.
    """
    return sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True)


class _ReadOnlyStoreView:
    """The minimal ``.connection`` view ``find_concomitant`` reads through (see its
    module docstring: it only ever issues SELECTs through ``store.connection``)."""

    def __init__(self, path: Path) -> None:
        self._connection = _open_readonly(path)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()


# --- the public entry point ------------------------------------------------------------

def run_concomitance(
    request: ConcomitanceRequest,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ConcomitanceRunResult:
    """Validate, run and write one concomitance request; return its summary.

    Raises ``ValueError`` if ``check_request`` finds an ERROR (a shell should already have
    blocked Run on this, but the app layer re-checks so no caller can bypass validation).
    ``should_cancel`` is polled once per anchor; on cancel the sink is closed cleanly and
    the partial count returned — a forensic tool never discards work silently by deleting
    a partial file.
    """
    problems = check_request(request)
    errors = tuple(p for p in problems if p.severity is Severity.ERROR)
    if errors:
        raise ValueError("; ".join(p.message for p in errors))
    warnings = tuple(p for p in problems if p.severity is Severity.WARNING)

    # WHY resolve() before comparing: case_2 naming the SAME file as case_1 by a
    # different spelling (a relative path, a symlink) is one case, not two — and the
    # one-store/two-store distinction decides whether the same-identity exclusion
    # applies, so getting it wrong would silently change which pairs are reported.
    second_case = request.case_2
    if (second_case is not None
            and second_case.resolve() == request.case_1.resolve()):
        second_case = None

    view_1 = _ReadOnlyStoreView(request.case_1)
    view_2 = _ReadOnlyStoreView(second_case) if second_case is not None else None
    sink = CsvMatchSink(request.output_path)
    sink.open()
    try:
        summary = find_concomitant(
            view_1,
            view_2,
            group_1=request.group_1,
            group_2=request.group_2,
            space_threshold_m=request.space_threshold_m,
            time_before=request.time_before,
            time_after=request.time_after,
            use_accuracy=request.use_accuracy,
            sink=sink,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    finally:
        sink.close()
        view_1.close()
        if view_2 is not None:
            view_2.close()

    return ConcomitanceRunResult(
        match_count=summary.match_count,
        candidate_count_1=summary.candidate_count_1,
        candidate_count_2=summary.candidate_count_2,
        output_path=request.output_path,
        warnings=warnings)
