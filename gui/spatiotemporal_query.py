"""Qt-free mapping seam for the spatiotemporal concomitance panel.

Defines:    ``DURATION_UNIT_NAMES`` / ``duration_from`` (the duration-unit vocabulary the
            four spin+unit-combo pairs share — one source of truth so the GUI and
            ``build_request`` can never disagree on what a unit means),
            ``split_identifiers`` (multi-value paste support for the criterion value
            field) and ``build_request(...)`` — panel inputs -> a
            ``model_atlas.analyze.concomitance_request.ConcomitanceRequest``.
Used by:    gui.spatiotemporal_controller (the Qt panel calls these and nothing else) and
            tests/gui/test_spatiotemporal_query.py.
Depends on: model_atlas.analyze.concomitance_request (ConcomitanceRequest / EntityGroup /
            Criterion / MatchMode) and the standard library only (datetime, pathlib). No
            Qt import, so the mapping logic is testable headless — the whole point of
            splitting it out.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Sequence

from model_atlas.analyze.concomitance_request import ConcomitanceRequest, EntityGroup, MatchMode

# --- temporal-threshold units --------------------------------------------------------
# The duration units the panel offers, each as the timedelta of ONE unit so a value*unit
# multiplication yields the threshold. Declared once here (single source of truth) so
# the four unit combos and ``duration_from`` can never disagree on the set.
DURATION_UNITS: dict[str, dt.timedelta] = {
    "seconds": dt.timedelta(seconds=1),
    "minutes": dt.timedelta(minutes=1),
    "hours": dt.timedelta(hours=1),
    "days": dt.timedelta(days=1),
}
# Ordered names for the unit combos (dict order is insertion order, kept explicit).
DURATION_UNIT_NAMES: tuple[str, ...] = ("seconds", "minutes", "hours", "days")


def duration_from(value: float, unit: str) -> dt.timedelta:
    """Turn a (value, unit) pair from the UI into a timedelta threshold.

    WHY validate the unit: an unrecognised unit string (a typo, or a combo that drifted
    from ``DURATION_UNITS``) would otherwise silently pick a wrong scale; raising at the
    boundary keeps the threshold honest. A negative value has no duration meaning, so it
    is rejected here too (``check_request`` also rejects it, but failing fast at the
    boundary gives a more specific message than the generic validation table entry).
    """
    if unit not in DURATION_UNITS:
        raise ValueError(
            f"unknown duration unit {unit!r}; allowed: {DURATION_UNIT_NAMES}")
    if value < 0:
        raise ValueError(f"duration value must be >= 0, got {value}")
    return DURATION_UNITS[unit] * value


def split_identifiers(text: str) -> tuple[str, ...]:
    """One free-typed field's tokens, in stated order (multi-value paste support).

    HOW: split on both commas and newlines, strip each token, drop the empties — so
    "a, b\\nc" and a trailing comma both yield the intended tokens. Used when Add is
    clicked on a criterion value typed (rather than picked from the vocabulary dropdown),
    so pasting several identifiers at once mints one chip per token in a single click.
    Order is preserved (dedup is NOT done here: the controller dedupes against the
    group's existing criteria, which is a group-level, not a parsing-level, concern).
    """
    tokens: list[str] = []
    for chunk in text.replace(",", "\n").splitlines():
        stripped = chunk.strip()
        if stripped:
            tokens.append(stripped)
    return tuple(tokens)


def build_request(
    *,
    case_1: str,
    case_2: str | None,
    group_1_criteria: Sequence[object],
    group_1_mode: MatchMode,
    group_2_criteria: Sequence[object],
    group_2_mode: MatchMode,
    space_threshold_m: float,
    time_before_value: float,
    time_before_unit: str,
    time_after_value: float,
    time_after_unit: str,
    use_accuracy: bool,
    output_path: str,
) -> ConcomitanceRequest:
    """Map the panel's raw inputs to a ``ConcomitanceRequest`` (the input -> request seam).

    ``case_2`` is ``None`` (or blank) when the "compare against a second case" box is
    unticked, matching ``ConcomitanceRequest.case_2``'s "both groups from case_1" meaning.
    Criterion-list-vs-vocabulary-token validation is deliberately left to
    ``check_request``/the T101 engine adapter, so there is ONE source of truth for what
    is a legal request; this seam only reshapes types.
    """
    case_2_text = (case_2 or "").strip()
    return ConcomitanceRequest(
        case_1=Path(case_1).expanduser(),
        case_2=Path(case_2_text).expanduser() if case_2_text else None,
        group_1=EntityGroup(criteria=tuple(group_1_criteria), mode=group_1_mode),
        group_2=EntityGroup(criteria=tuple(group_2_criteria), mode=group_2_mode),
        space_threshold_m=space_threshold_m,
        time_before=duration_from(time_before_value, time_before_unit),
        time_after=duration_from(time_after_value, time_after_unit),
        use_accuracy=use_accuracy,
        output_path=Path(output_path).expanduser(),
    )
