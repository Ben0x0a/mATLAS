"""Case vocabulary: the selection tokens actually present in one case, with counts.

Defines:    ``VocabularyEntry`` (one selectable value + its optional qualifier + how many
            queryable placements carry it) and ``read_case_vocabulary(db_path) -> dict[
            CriterionKind, tuple[VocabularyEntry, ...]]`` — one entry list per
            ``CriterionKind``, so an operator can never be offered a token that is not
            actually present in this evidence.
Used by:    ``gui.spatiotemporal_controller`` (populates the two groups' criterion value
            dropdowns) and, in T101b, ``launcher.cli``.
Depends on: ``model_atlas.analyse.concomitance`` (``CriterionKind``),
            ``model_atlas.analyse.concomitance`` (``FROM_JOINS`` — reused so the join
            that defines "a queryable placement" can never drift from the engine's own
            candidate definition; READ-ONLY import, ``concomitance.py`` is not edited),
            and the standard library only (``sqlite3``, ``dataclasses``, ``pathlib``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from model_atlas.analyse.helpers.queries import FROM_JOINS

# "A queryable placement" = an observation with BOTH an integrated point and an integrated
# time bound (task wording). Deliberately narrower than the engine's full presence
# predicate (concomitance._PRESENCE_WHERE), which ALSO excludes non-presence links
# ('references' pins, 'never_during_interval'): the vocabulary answers "what CAN be
# selected", not "what would survive the presence filter" — a value kept out of a result
# only by its observation's link is still a legitimate, selectable token here.
_PLACEMENT_WHERE = (
    " WHERE ti.lower_unix_utc_us IS NOT NULL AND ti.upper_unix_utc_us IS NOT NULL"
    " AND si.latitude_wgs84 IS NOT NULL AND si.longitude_wgs84 IS NOT NULL"
)



class CriterionKind(Enum):
    """What one criterion selects on — the three axes this module can enumerate.

    It lives HERE, beside the queries that answer it, rather than in the concomitance
    request layer: the axes are a property of the case vocabulary, and concomitance is one
    consumer that selects on them. Declaring it the other way round made the vocabulary
    reader import the concomitance engine to answer "which tokens exist in this case".
    """

    ENTITY_TYPE = "entity_type"
    IDENTIFIER_TYPE = "identifier_type"
    IDENTIFIER = "identifier"

@dataclass(frozen=True)
class VocabularyEntry:
    """One selectable value: its qualifier (an identifier's identifier_type; else None)
    and how many queryable placements (observations with both a position and a time) carry
    it — never the raw entity count, which would count entities with no integrated point
    and mislead the operator with a promising number that can never appear in a result.
    """

    value: str
    qualifier: str | None
    placement_count: int


def _open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` read-only via a percent-encoded ``file:`` URI (mode=ro).

    HOW: ``Path.as_uri()`` percent-encodes the folder BEFORE ``?mode=ro`` is appended, so
    a folder carrying a URI-significant character ('#', '?', '%') is never mis-parsed by
    SQLite's URI parser into a bogus path + fragment (the same pattern as
    ``gui.spatiotemporal_controller._PersistedRunView``, copied here rather than shared
    across a Qt/Qt-free boundary). WHY read-only: a case is evidence — reading its
    vocabulary must never be able to mutate it.
    """
    return sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True)


def _entries(connection: sqlite3.Connection, value_column: str, group_by: str,
             qualifier_column: str | None = None) -> tuple[VocabularyEntry, ...]:
    """One ``CriterionKind``'s entries: one row per distinct (value[, qualifier]),
    counted over placements, sorted by count descending then value ascending —
    deterministic, and the useful (most-attested) entries lead.
    """
    qualifier_select = f", {qualifier_column}" if qualifier_column else ""
    rows = connection.execute(
        f"SELECT {value_column} AS value{qualifier_select}, COUNT(*) AS placement_count"
        + FROM_JOINS + _PLACEMENT_WHERE
        + f" AND {value_column} IS NOT NULL"
        + f" GROUP BY {group_by}"
        + " ORDER BY placement_count DESC, value ASC"
    ).fetchall()
    if qualifier_column:
        return tuple(
            VocabularyEntry(value=value, qualifier=qualifier, placement_count=count)
            for value, qualifier, count in rows)
    return tuple(
        VocabularyEntry(value=value, qualifier=None, placement_count=count)
        for value, count in rows)


def read_case_vocabulary(
        db_path: str | Path) -> dict[CriterionKind, tuple[VocabularyEntry, ...]]:
    """Every value actually present in ``db_path``, one entry tuple per ``CriterionKind``.

    Three independent queries (one per kind) over the SAME join as the engine's own
    candidate load, so a value offered here is guaranteed queryable. A kind with no
    matching placements at all in this case yields an EMPTY tuple (never falls back to
    the full static vocabulary enum — offering an unused token would let an operator pick
    something that can only ever return zero results).
    """
    connection = _open_readonly(db_path)
    try:
        return {
            CriterionKind.ENTITY_TYPE: _entries(connection, "e.type", "e.type"),
            CriterionKind.IDENTIFIER_TYPE: _entries(
                connection, "e.identifier_type", "e.identifier_type"),
            CriterionKind.IDENTIFIER: _entries(
                connection, "e.identifier", "e.identifier, e.identifier_type",
                qualifier_column="e.identifier_type"),
        }
    finally:
        connection.close()
