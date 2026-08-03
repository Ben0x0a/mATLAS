"""The concomitance app-layer orchestrator — the ONLY thing either shell calls to run one.

Defines:    ``ConcomitanceNotYetSupported`` (the one loud, precise capability-gap error),
            ``ConcomitanceRunResult`` (the stable result shape) and ``run_concomitance(
            request, *, on_progress=None, should_cancel=None) -> ConcomitanceRunResult`` —
            owns validate -> open store(s) -> run the engine -> stream to the sink ->
            close everything -> return a summary.
Used by:    ``gui.spatiotemporal_controller`` (through ``TaskRunner``/``ProgressBridge``)
            and, in T101b, ``launcher.cli``'s concomitance subcommand. Neither callback
            parameter is Qt: both are plain callables, so this module stays importable
            with no GUI toolkit installed.
Depends on: ``model_atlas.analyze.concomitance_request`` (``ConcomitanceRequest`` /
            ``check_request`` / ``EntityGroup`` / ``MatchMode`` / ``CriterionKind`` /
            ``Problem`` / ``Severity`` / ``same_selection``), ``model_atlas.analyze.
            concomitance_output`` (``MatchRow`` / ``CsvMatchSink``), ``model_atlas.analyze.
            concomitance`` (``EntitySelector`` / ``find_concomitant`` — the T102-pending
            engine, called through its CURRENT symmetric signature and never edited), and
            the standard library only (``sqlite3``, ``dataclasses``, ``pathlib``,
            ``typing``).

The engine seam (read this before changing anything here)
-----------------------------------------------------------
``find_concomitant`` still has its OLD symmetric signature: one selector, one
threshold, one case. T102 rewrites it; T101 must not. ``_ensure_engine_can_run`` is
therefore the ONE function that maps a ``ConcomitanceRequest`` onto what the engine can
do today and raises ``ConcomitanceNotYetSupported`` — loudly, never approximated — for
everything it cannot: two different entity groups, an asymmetric time window,
accuracy-aware matching, and a second case. T102 deletes every check in this one
function when the engine gains the capability.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from model_atlas.analyze.concomitance import ConcomitantPair, EntitySelector, find_concomitant
from model_atlas.analyze.concomitance_output import CsvMatchSink, MatchRow
from model_atlas.analyze.concomitance_request import (
    ConcomitanceRequest,
    CriterionKind,
    EntityGroup,
    MatchMode,
    Problem,
    Severity,
    check_request,
    same_selection,
)


class ConcomitanceNotYetSupported(Exception):
    """A request needs the T102 engine rewrite. Caught by both shells and shown as a
    normal message (never a traceback) — see the module docstring's engine seam."""


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


# --- the group -> engine-selector translation (part of the ONE adapter function) -------

def _entity_selector_for(group: EntityGroup) -> EntitySelector:
    """Translate one ``EntityGroup`` into the engine's flat ``EntitySelector``.

    The T101 engine ANDs its three axes (type / identifier_type / identifier) and treats
    several values WITHIN one axis as a union (an ``IN`` list) — exactly ``MatchMode.ANY``
    limited to ONE kind, or ``MatchMode.ALL`` with at most one value per kind. A group that
    needs a genuine cross-kind OR (``ANY`` spanning several kinds) or a genuine same-kind
    intersection (``ALL`` with more than one value for one kind) asks the engine for
    something it structurally cannot express, so it is refused here rather than silently
    approximated into the wrong axis combination.
    """
    buckets: dict[CriterionKind, list[str]] = {kind: [] for kind in CriterionKind}
    for criterion in group.criteria:
        buckets[criterion.kind].append(criterion.value)
    kinds_used = [kind for kind, values in buckets.items() if values]
    if group.mode is MatchMode.ANY and len(kinds_used) > 1:
        raise ConcomitanceNotYetSupported(
            "Matching 'any' of criteria spanning more than one kind needs the "
            "T102 engine.")
    if group.mode is MatchMode.ALL and any(len(values) > 1 for values in buckets.values()):
        raise ConcomitanceNotYetSupported(
            "Matching 'all' of more than one value for the same kind needs the "
            "T102 engine.")
    return EntitySelector(
        types=tuple(buckets[CriterionKind.ENTITY_TYPE]),
        identifier_types=tuple(buckets[CriterionKind.IDENTIFIER_TYPE]),
        identifiers=tuple(buckets[CriterionKind.IDENTIFIER]))


def _ensure_engine_can_run(request: ConcomitanceRequest) -> EntitySelector:
    """The ONE function housing every T101->T102 capability-gap check (see the module
    docstring's engine seam table). Returns the translated selector on the supported
    shape; raises ``ConcomitanceNotYetSupported`` on everything else.
    """
    if request.case_2 is not None:
        raise ConcomitanceNotYetSupported("Comparing two cases needs the T102 engine.")
    if request.time_before != request.time_after:
        raise ConcomitanceNotYetSupported(
            "An asymmetric time window needs the T102 engine.")
    if request.use_accuracy:
        raise ConcomitanceNotYetSupported("Accuracy-aware matching needs the T102 engine.")
    if not same_selection(request.group_1, request.group_2):
        raise ConcomitanceNotYetSupported(
            "Two different entity groups need the T102 engine.")
    return _entity_selector_for(request.group_1)


# --- enriching an engine pair into a full MatchRow (lat/lon/time are NOT on the pair) --

# The engine's ConcomitantPair carries only identity + the two exact measures (see its
# module docstring: "absent from today's output" for position/time). Both sides' where/
# when/traceability are fetched here, by assertion id, over the SAME integration tables
# the engine itself joins through.
_SIDE_SQL = (
    "SELECT si.latitude_wgs84, si.longitude_wgs84, si.horizontal_accuracy_m,"
    " ti.lower_unix_utc_us, ti.upper_unix_utc_us,"
    " ti.lower_datetime_iso, ti.upper_datetime_iso, a.source_label"
    " FROM assertion a"
    " JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id"
    " JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id"
    " WHERE a.assertion_id = ?"
)


@dataclass(frozen=True)
class _SideDetails:
    latitude: float
    longitude: float
    accuracy_m: float | None
    lower_us: int
    upper_us: int
    time_start: str | None
    time_end: str | None
    source: str | None


def _side_details(connection: sqlite3.Connection, assertion_id: str) -> _SideDetails:
    row = connection.execute(_SIDE_SQL, (assertion_id,)).fetchone()
    # Invariant: the pair came from find_concomitant's own candidate set, which requires
    # exactly these joins to succeed — a miss here means the pair and this query
    # disagree on what a candidate is, never a normal outcome.
    assert row is not None, (
        f"assertion {assertion_id} matched find_concomitant but has no integrated "
        f"point/time: the candidate definitions drifted")
    (latitude, longitude, accuracy_m, lower_us, upper_us,
     time_start, time_end, source) = row
    return _SideDetails(latitude, longitude, accuracy_m, lower_us, upper_us,
                        time_start, time_end, source)


def _signed_time_delta_s(g1: _SideDetails, g2: _SideDetails) -> tuple[float, bool]:
    """The signed gap in seconds (negative = group 2 earlier) and whether they overlap.

    HOW: mirrors the engine's own interval-gap arithmetic (``_interval_gap_us``) but
    keeps the SIGN instead of clamping to a magnitude: if g1 finished before g2 started,
    g2 is later (positive); if g2 finished before g1 started, g2 is earlier (negative);
    otherwise the intervals overlap (zero, ``time_overlap=True``).
    """
    if g1.lower_us > g2.upper_us:
        return -(g1.lower_us - g2.upper_us) / 1_000_000.0, False
    if g2.lower_us > g1.upper_us:
        return (g2.lower_us - g1.upper_us) / 1_000_000.0, False
    return 0.0, True


def _to_match_row(connection: sqlite3.Connection, pair: ConcomitantPair) -> MatchRow:
    """One engine pair, enriched into the full output row.

    ``distance_within_accuracy`` is always False on this T101 adapter: the supported
    shape has ``use_accuracy=False`` by construction (``_ensure_engine_can_run`` refuses
    the request otherwise), so accuracy never plays a part in why a pair matched.
    """
    g1 = _side_details(connection, pair.assertion_a)
    g2 = _side_details(connection, pair.assertion_b)
    time_delta_s, overlap = _signed_time_delta_s(g1, g2)
    return MatchRow(
        g1_identifier=pair.entity_a.identifier,
        g1_identifier_type=pair.entity_a.identifier_type,
        g1_entity_type=pair.entity_a.type,
        g1_latitude=g1.latitude, g1_longitude=g1.longitude, g1_accuracy_m=g1.accuracy_m,
        g1_time_start=g1.time_start, g1_time_end=g1.time_end,
        g1_assertion_id=pair.assertion_a, g1_source=g1.source,
        g2_identifier=pair.entity_b.identifier,
        g2_identifier_type=pair.entity_b.identifier_type,
        g2_entity_type=pair.entity_b.type,
        g2_latitude=g2.latitude, g2_longitude=g2.longitude, g2_accuracy_m=g2.accuracy_m,
        g2_time_start=g2.time_start, g2_time_end=g2.time_end,
        g2_assertion_id=pair.assertion_b, g2_source=g2.source,
        distance_m=pair.distance_m, distance_within_accuracy=False,
        time_delta_s=time_delta_s, time_overlap=overlap)


# --- the public entry point ------------------------------------------------------------

def run_concomitance(
    request: ConcomitanceRequest,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ConcomitanceRunResult:
    """Validate, run and write one concomitance request; return its summary.

    Raises ``ValueError`` if ``check_request`` finds an ERROR (a shell should already have
    blocked Run on this, but the app layer re-checks so no caller can bypass validation),
    and ``ConcomitanceNotYetSupported`` if the request needs the T102 engine (see the
    module docstring). ``should_cancel`` is polled once per anchor pair; on cancel the
    sink is closed cleanly and the partial count returned — a forensic tool never
    discards work silently by deleting a partial file.
    """
    problems = check_request(request)
    errors = tuple(p for p in problems if p.severity is Severity.ERROR)
    if errors:
        raise ValueError("; ".join(p.message for p in errors))
    warnings = tuple(p for p in problems if p.severity is Severity.WARNING)

    selector = _ensure_engine_can_run(request)

    view = _ReadOnlyStoreView(request.case_1)
    try:
        result = find_concomitant(
            view,
            entity_selector=selector,
            space_threshold_m=request.space_threshold_m,
            # before == after is guaranteed by _ensure_engine_can_run above.
            time_threshold=request.time_before,
        )
        sink = CsvMatchSink(request.output_path)
        sink.open()
        match_count = 0
        try:
            total = len(result.pairs)
            for done, pair in enumerate(result.pairs, start=1):
                if should_cancel is not None and should_cancel():
                    break
                sink.write(_to_match_row(view.connection, pair))
                match_count += 1
                if on_progress is not None:
                    on_progress(done, total)
        finally:
            sink.close()
    finally:
        view.close()

    return ConcomitanceRunResult(
        match_count=match_count,
        candidate_count_1=result.candidate_count,
        candidate_count_2=result.candidate_count,
        output_path=request.output_path,
        warnings=warnings)
