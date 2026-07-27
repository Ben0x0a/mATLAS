"""Qt-free mapping seams for the spatiotemporal concomitance panel.

Defines:    the two pure functions the GUI panel is built on, kept free of any Qt
            import so they unit-test without a running application:
            (a) UI inputs -> a ``ConcomitanceQuery`` (an ``EntitySelector`` plus the
            ``find_concomitant`` threshold kwargs) via ``build_query`` — with the
            ``split_identifiers`` / ``duration_from`` input parsers and the
            ``DURATION_UNIT_NAMES`` the unit combo is populated from; and
            (b) a ``ResultSet`` -> display table rows (``ConcomitanceRow``) via
            ``result_to_rows``, with the ``format_*`` cell formatters and the
            ``RESULT_COLUMNS`` header the table view labels its columns with.
Used by:    gui.spatiotemporal_controller (the Qt panel calls these) and
            tests/gui/test_spatiotemporal_query.py.
Depends on: model_atlas.analyze.concomitance (EntitySelector / ResultSet / EntityRef)
            and the standard library only (datetime). No Qt import, so the mapping
            logic is testable headless — the whole point of splitting it out.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from model_atlas.analyze.concomitance import (
    EntityRef,
    EntitySelector,
    ResultSet,
)

# --- temporal-threshold units -------------------------------------------------
# The duration units the panel offers, each as the timedelta of ONE unit so a
# value*unit multiplication yields the threshold. Declared once here (single source
# of truth) so the combo box and ``duration_from`` can never disagree on the set.
DURATION_UNITS: dict[str, dt.timedelta] = {
    "seconds": dt.timedelta(seconds=1),
    "minutes": dt.timedelta(minutes=1),
    "hours": dt.timedelta(hours=1),
    "days": dt.timedelta(days=1),
}
# Ordered names for the unit combo (dict order is insertion order, kept explicit).
DURATION_UNIT_NAMES: tuple[str, ...] = ("seconds", "minutes", "hours", "days")


def duration_from(value: float, unit: str) -> dt.timedelta:
    """Turn a (value, unit) pair from the UI into a timedelta threshold.

    WHY validate the unit: an unrecognised unit string (a typo, or a combo that
    drifted from ``DURATION_UNITS``) would otherwise silently pick a wrong scale;
    raising at the boundary keeps the threshold honest. A negative value has no
    duration meaning, so it is rejected here as well as by ``find_concomitant``.
    """
    if unit not in DURATION_UNITS:
        raise ValueError(
            f"unknown duration unit {unit!r}; allowed: {DURATION_UNIT_NAMES}")
    if value < 0:
        raise ValueError(f"temporal threshold must be >= 0, got {value}")
    return DURATION_UNITS[unit] * value


def split_identifiers(text: str) -> tuple[str, ...]:
    """The explicit identifier list from one free-text field, in stated order.

    HOW: split on both commas and newlines, strip each token, drop the empties —
    so "a, b\\nc" and a trailing comma both yield the intended tokens. Order is
    preserved (dedup is NOT done: a repeated identifier is the operator's choice and
    ``find_concomitant`` treats the axis as a set anyway).
    """
    tokens: list[str] = []
    for chunk in text.replace(",", "\n").splitlines():
        stripped = chunk.strip()
        if stripped:
            tokens.append(stripped)
    return tuple(tokens)


@dataclass(frozen=True)
class ConcomitanceQuery:
    """A fully-formed ``find_concomitant`` call: the selector and both thresholds.

    ``kwargs`` hands the exact keyword arguments to ``find_concomitant`` so the
    controller's worker never re-derives them — the mapping is done once, here.
    """

    entity_selector: EntitySelector
    space_threshold_m: float
    time_threshold: dt.timedelta

    def kwargs(self) -> dict[str, object]:
        return {
            "entity_selector": self.entity_selector,
            "space_threshold_m": self.space_threshold_m,
            "time_threshold": self.time_threshold,
        }


def build_query(
    *,
    types: object,
    identifier_types: object,
    identifiers_text: str,
    space_threshold_m: float,
    time_value: float,
    time_unit: str,
) -> ConcomitanceQuery:
    """Map the panel's raw inputs to a ``ConcomitanceQuery`` (the input->params seam).

    ``types`` / ``identifier_types`` are the tokens selected in the two vocabulary
    lists (any iterable of strings); ``identifiers_text`` is the free-text explicit
    list. Empty axes stay empty, which ``find_concomitant`` reads as "no constraint"
    (narrowing is opt-in). Vocabulary-token validation is deliberately left to
    ``find_concomitant`` so there is ONE source of truth for the allowed tokens and
    its unknown-token error message reaches the user unchanged.
    """
    selector = EntitySelector(
        types=tuple(types),
        identifier_types=tuple(identifier_types),
        identifiers=split_identifiers(identifiers_text),
    )
    return ConcomitanceQuery(
        entity_selector=selector,
        space_threshold_m=space_threshold_m,
        time_threshold=duration_from(time_value, time_unit),
    )


# --- ResultSet -> table rows --------------------------------------------------
# The column order the task fixes: the two entities, the two exact measures, then
# the two source assertion ids (so every row is traceable back to evidence).
RESULT_COLUMNS: tuple[str, ...] = (
    "Entity A", "Entity B", "Distance (m)", "Time gap", "Assertion A", "Assertion B",
)


def format_entity(ref: EntityRef) -> str:
    """One entity as a cell: its working identity, or a loud unidentified marker.

    A NULL identifier is a purely typed mention (identified only by its type, with
    no concrete identifier value) that can never
    converge, so it is shown as an em dash rather than blank — "no identity" must
    read differently from an empty cell. The type/identifier_type qualifier lets an
    analyst tell two unidentified devices apart at a glance.
    """
    identity = ref.identifier if ref.identifier else "—"
    return f"{identity} ({ref.identifier_type}/{ref.type})"


def format_distance_m(distance_m: float) -> str:
    """Great-circle distance to one decimal metre (the engine's exact value)."""
    return f"{distance_m:.1f}"


def format_time_gap_us(time_gap_us: int) -> str:
    """The interval gap as a compact human duration; overlap is called out.

    HOW: split the µs into d/h/m/s, dropping leading zero units but always keeping a
    seconds field (with sub-second decimals only when present, since demo gaps are
    whole seconds). WHY name the overlap: a gap of 0 means the two intervals touch or
    overlap — a materially different finding from "1 second apart" — so it is
    labelled rather than shown as a bare "0s".
    """
    if time_gap_us == 0:
        return "0s (overlap)"
    seconds, micro = divmod(time_gap_us, 1_000_000)
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s" if micro == 0 else f"{secs + micro / 1_000_000:.3f}s")
    return " ".join(parts)


@dataclass(frozen=True)
class ConcomitanceRow:
    """One display row of the results table (all cells pre-formatted strings)."""

    entity_a: str
    entity_b: str
    distance_m: str
    time_gap: str
    assertion_a: str
    assertion_b: str

    def as_cells(self) -> tuple[str, ...]:
        """The cells in ``RESULT_COLUMNS`` order — the table model reads these."""
        return (self.entity_a, self.entity_b, self.distance_m, self.time_gap,
                self.assertion_a, self.assertion_b)


def result_to_rows(result: ResultSet) -> list[ConcomitanceRow]:
    """Map a ``ResultSet`` to display rows (the result->table-model seam).

    The pairs are already sorted deterministically by (assertion_a, assertion_b) in
    the engine, so the row order is stable across runs without re-sorting here.
    """
    return [
        ConcomitanceRow(
            entity_a=format_entity(pair.entity_a),
            entity_b=format_entity(pair.entity_b),
            distance_m=format_distance_m(pair.distance_m),
            time_gap=format_time_gap_us(pair.time_gap_us),
            assertion_a=pair.assertion_a,
            assertion_b=pair.assertion_b,
        )
        for pair in result.pairs
    ]
