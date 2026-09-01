"""The concomitance request shape: criteria, groups, thresholds, and validation.

Defines:    ``CriterionKind`` / ``Criterion`` (what a criterion selects on), ``MatchMode``
            (how criteria inside ONE group combine), ``EntityGroup`` (a criteria list +
            mode), ``ConcomitanceRequest`` (the full asymmetric two-group question),
            ``Severity`` / ``Problem`` and ``check_request(request) -> tuple[Problem, ...]``
            — the ONE validator both shells call, so the CLI (T101b) and the GUI can never
            disagree on what is legal.
Used by:    ``model_atlas.app.concomitance_run`` (validates before running, and re-uses
            ``same_selection`` for the T101 engine-support check), ``gui.spatiotemporal_query``
            (builds a ``ConcomitanceRequest`` from panel state) and, in T101b,
            ``launcher.cli``'s concomitance subcommand.
Depends on: the standard library only (dataclasses, datetime, enum, os, pathlib) — no Qt,
            no sqlite3, no I/O beyond a writability probe on the output folder, so this
            module is usable headless by both shells and every unit test.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from model_atlas.analyse.helpers import CriterionKind


# --- the duration vocabulary (shared by BOTH shells) ---------------------------------
# One unit -> the timedelta of ONE of it, so value*unit yields the threshold. This lives
# in the shared request layer, not in either shell: the GUI's unit combos and the CLI's
# --before/--after suffixes must offer exactly the same set, and a shell importing another
# shell to get it would invert the dependency direction (shells depend on the layers
# below them, never on each other).
DURATION_UNITS: dict[str, dt.timedelta] = {
    "seconds": dt.timedelta(seconds=1),
    "minutes": dt.timedelta(minutes=1),
    "hours": dt.timedelta(hours=1),
    "days": dt.timedelta(days=1),
}
# Ordered names for the GUI's unit combos (dict order is insertion order, kept explicit).
DURATION_UNIT_NAMES: tuple[str, ...] = ("seconds", "minutes", "hours", "days")


def duration_from(value: float, unit: str) -> dt.timedelta:
    """Turn a (value, unit) pair from either shell into a timedelta threshold.

    WHY validate the unit: an unrecognised unit string (a typo, or a combo/suffix table
    that drifted from ``DURATION_UNITS``) would otherwise silently pick a wrong scale;
    raising at the boundary keeps the threshold honest. A negative value has no duration
    meaning, so it is rejected here too (``check_request`` also rejects it, but failing
    fast at the boundary gives a more specific message than the validation table entry).
    """
    if unit not in DURATION_UNITS:
        raise ValueError(
            f"unknown duration unit {unit!r}; allowed: {DURATION_UNIT_NAMES}")
    if value < 0:
        raise ValueError(f"duration value must be >= 0, got {value}")
    return DURATION_UNITS[unit] * value

@dataclass(frozen=True)
class Criterion:
    """One selection token: a kind + the literal value (as stored in the case)."""

    kind: CriterionKind
    value: str


class MatchMode(Enum):
    """How the criteria INSIDE one group combine."""

    ANY = "any"    # union — the list IS the group (default)
    ALL = "all"    # intersection — today's AND-ed behaviour


@dataclass(frozen=True)
class EntityGroup:
    """Which entities take part on one side. Empty criteria = every entity in the case.

    Narrowing is opt-in (house rule): an untouched group constrains nothing.
    """

    criteria: tuple[Criterion, ...] = ()
    mode: MatchMode = MatchMode.ANY


def same_selection(group_a: EntityGroup, group_b: EntityGroup) -> bool:
    """True when two groups resolve to the SAME criteria + mode (order-independent).

    Used both by ``check_request`` (the identical-groups WARNING) and by
    ``model_atlas.app.concomitance_run`` (the T101 engine-support check needs literally
    the same test: today's engine only runs a SINGLE symmetric selector, which is exactly
    what "the two groups resolve to the same criteria+mode" means). Kept in ONE place so
    the two callers can never drift apart on what "the same selection" means.

    Criteria are compared as a set (not tuple order) because an operator adding the same
    two criteria in a different order plainly means the same selection.
    """
    return group_a.mode is group_b.mode and set(group_a.criteria) == set(group_b.criteria)


@dataclass(frozen=True)
class ConcomitanceRequest:
    """The full asymmetric concomitance question: two independently-chosen groups, an
    optional second case, a distance threshold and a directional time window.

    ``case_2`` is ``None`` when both groups are drawn from ``case_1``. ``time_before`` is
    how much EARLIER group 2 may be than group 1; ``time_after`` how much LATER.
    """

    case_1: Path
    case_2: Path | None
    group_1: EntityGroup
    group_2: EntityGroup
    space_threshold_m: float
    time_before: dt.timedelta
    time_after: dt.timedelta
    output_path: Path
    use_accuracy: bool = False


class Severity(Enum):
    ERROR = "error"       # blocks the run
    WARNING = "warning"   # shown, but the run may proceed


@dataclass(frozen=True)
class Problem:
    """One validation finding: a severity plus the operator-facing message."""

    severity: Severity
    message: str


def check_request(request: ConcomitanceRequest) -> tuple[Problem, ...]:
    """Every validation finding for ``request`` — the ONE source of truth both shells call.

    Checks, in the order the task's table lists them (see the module docstring context in
    tasks/T101a.md): a negative distance or time window is an ERROR; both thresholds at
    zero is an ERROR (that configuration matches nothing); two groups in ONE case that
    resolve to the same selection is a WARNING (not a block — a PARTIAL overlap is
    legitimate, only a fully identical selection is futile, and it must be surfaced
    before the run rather than discovered as an empty file that reads as "nothing found"
    when the truth is "this configuration cannot find anything"); an unwritable output
    folder is an ERROR.
    """
    problems: list[Problem] = []

    if request.space_threshold_m < 0:
        problems.append(Problem(Severity.ERROR, "Distance threshold must be 0 m or more."))
    if request.time_before < dt.timedelta(0) or request.time_after < dt.timedelta(0):
        problems.append(Problem(Severity.ERROR, "Time windows must be 0 or more."))
    if (request.space_threshold_m == 0
            and request.time_before == dt.timedelta(0)
            and request.time_after == dt.timedelta(0)):
        problems.append(Problem(
            Severity.ERROR,
            "Set a distance or a time window; both zero matches nothing."))
    if request.case_2 is None and same_selection(request.group_1, request.group_2):
        problems.append(Problem(
            Severity.WARNING,
            "Group 1 and Group 2 select the same entities in one case, so every pair "
            "would be excluded as the same identity. To ask where one entity repeatedly "
            "appears, use a hotspot analysis rather than concomitance."))
    parent = request.output_path.parent
    # os.access is the cheap, no-write-attempted way to ask "could I write here" — a
    # missing parent folder or a read-only one both fail it, and neither should be
    # discovered only once the engine has already run.
    if not (parent.is_dir() and os.access(parent, os.W_OK)):
        problems.append(Problem(
            Severity.ERROR, f"Cannot write results to {request.output_path}."))

    return tuple(problems)
