"""Unit tests for the core run-DB vocabularies and family declarations.

Defines:    a round-trip test proving every enum-valued string in the worked-example
            demo DB is a member of the corresponding enum (columns enumerated
            dynamically, no hardcoded row lists), the needs_bearer/is_presence derivations,
            the phrase-fragment tests (including the negation/reference strings), and
            the is_standard_part predicate test.
Used by:    pytest.
Depends on: model_atlas.core.vocabularies and docs/examples/matlas_example.sqlite.
"""
from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path

import pytest

from model_atlas.core.vocabularies import (
    ActivityKind,
    EditKind,
    ActivityRole,
    Attestation,
    EntityLink,
    EntityType,
    Occupancy,
    Origin,
    RecordState,
    SourceTier,
    SpatialReferential,
    SpatialLink,
    TemporalKind,
    TemporalReferential,
    column_cast,
    is_standard_part,
)

# The T00 demo DB (read-only): every enum-valued string it stores must round-trip.
_DEMO_DB = (
    Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"
)

# (table, column) -> the enum whose members must contain every stored value. The
# columns are the enum-valued (closed-vocabulary) ones; free-text/method-defined columns
# (e.g. analysis.record_rank_type, activity.context_json, entity.identifier_type — a free string
# whose known kinds IdentifierType only suggests) are deliberately excluded.
_ENUM_COLUMNS: dict[tuple[str, str], type[Enum]] = {
    ("entity", "type"): EntityType,
    ("entity", "concerns_origin"): Origin,
    ("observation", "entity_origin"): Origin,
    ("observation", "activity_role"): ActivityRole,
    ("observation", "entity_link"): EntityLink,
    ("observation", "spatial_link"): SpatialLink,
    ("observation", "occupancy"): Occupancy,
    ("observation", "attestation"): Attestation,
    ("observation", "deleted"): RecordState,
    ("spatial_expression", "referential"): SpatialReferential,
    ("temporal_expression", "kind"): TemporalKind,
    ("temporal_expression", "referential"): TemporalReferential,
    ("activity", "kind"): ActivityKind,
    ("raw_record", "recovery_state"): RecordState,
    ("edit_log", "edit_kind"): EditKind,
    ("source", "source_tier"): SourceTier,
}


def _distinct_values(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    # HOW: read the distinct non-NULL values actually present, so the fixture drives
    # the observation rather than a hardcoded list that could drift from the DB.
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
    ).fetchall()
    return [r[0] for r in rows]


@pytest.mark.skipif(not _DEMO_DB.exists(), reason="demo DB fixture absent")
@pytest.mark.parametrize(("table", "column", "enum"), [
    (table, column, enum) for (table, column), enum in _ENUM_COLUMNS.items()
])
def test_demo_db_enum_values_round_trip(table: str, column: str, enum: type[Enum]) -> None:
    # Every value the demo DB stores in an enum-valued column must construct its enum;
    # a stray string (a typo, a dropped member) would raise ValueError here.
    conn = sqlite3.connect(f"file:{_DEMO_DB}?mode=ro", uri=True)
    try:
        values = _distinct_values(conn, table, column)
    finally:
        conn.close()
    assert values, f"{table}.{column} had no values to check"
    for value in values:
        enum(value)  # raises ValueError if not a member


def test_demo_db_component_parts_are_standard() -> None:
    # Decision 23: the demo uses only standard part names; is_standard_part must accept
    # every one. (Ingest accepts any value; only standard names are export-projected.)
    conn = sqlite3.connect(f"file:{_DEMO_DB}?mode=ro", uri=True)
    try:
        spatial = _distinct_values(conn, "spatial_component", "part")
        temporal = _distinct_values(conn, "temporal_component", "part")
    finally:
        conn.close()
    assert spatial and temporal
    for part in spatial + temporal:
        assert is_standard_part(part), f"{part!r} should be a standard part"


# --- The two surviving derivations ----------------------------------------
# Genesis/Locates are GONE with the conflated EntityPositionLink/EntityTimeLink they
# un-mixed. What replaces them reads a SINGLE orthogonal field each, so neither can lose
# information the way those views did (``at`` and ``claimed_at`` both rendered ``self``).

def test_needs_bearer_is_exactly_not_self() -> None:
    """The bearer invariant: a bearer row exists iff the link is not ``self``.

    Every member must resolve, so a future addition cannot silently lack a rule.
    """
    assert {link: link.needs_bearer for link in EntityLink} == {
        EntityLink.SELF: False,
        EntityLink.SERVED_BY: True,
        EntityLink.SEEN_BY: True,
        EntityLink.USED_AT: True,
        EntityLink.STAYED_AT: True,
        EntityLink.SEARCHED_FOR: True,
        EntityLink.SHARED: True,
        EntityLink.CACHED: True,
        EntityLink.UNKNOWN: True,
    }


def test_is_presence_separates_the_three_negatives() -> None:
    """The ONE presence predicate, replacing a three-token test across two enums.

    The point of the split: ``references`` (interest, no presence), ``absent`` (asserted
    non-presence) and ``unknown`` (no claim either way) are all NOT presence, but they are
    not each other — and ``unknown`` must stay distinguishable so it can be surfaced for
    review rather than silently dropped.
    """
    assert {link: link.is_presence for link in SpatialLink} == {
        SpatialLink.AT: True,
        SpatialLink.WITHIN: True,
        SpatialLink.NEAR: True,
        SpatialLink.REFERENCES: False,
        SpatialLink.ABSENT: False,
        SpatialLink.UNKNOWN: False,
    }
    assert len({SpatialLink.REFERENCES, SpatialLink.ABSENT, SpatialLink.UNKNOWN}) == 3


def test_negation_lives_only_on_the_spatial_link() -> None:
    """Polarity on space, quantification on time — the convention modal duality forces.

    NOT-diamond-P == box-NOT-P, so permitting negation on both corners would let one fact be
    written two ways. That was the real bug: ``NEVER_DURING_INTERVAL`` existed on
    ``EntityTimeLink`` AND ``SpatialTemporalLink`` at once, with no rule for disagreement.
    """
    assert not any("absent" in m.value or "never" in m.value for m in Occupancy)
    assert SpatialLink.ABSENT.value == "absent"


def test_occupancy_has_no_instant_member() -> None:
    """An instant is the degenerate interval ``lower == upper``, where box == diamond.

    The shape of the expression is ``TemporalKind``'s job; occupancy would carry no
    information there, so the member must not exist.
    """
    assert not any(m.value == "instant" for m in Occupancy)
    assert any(m.value == "instant" for m in TemporalKind)


# --- Phrase fragments (decision 37) ---------------------------------------

_PHRASED_ENUMS = (
    EntityLink,
    SpatialLink,
    Occupancy,
    Attestation,
    ActivityRole,
    TemporalKind,
)


@pytest.mark.parametrize("enum", _PHRASED_ENUMS)
def test_every_phrased_member_has_a_non_empty_phrase(enum: type[Enum]) -> None:
    for member in enum:
        assert isinstance(member.phrase, str) and member.phrase.strip(), (
            f"{enum.__name__}.{member.name} has an empty phrase"
        )


def test_negation_and_reference_members_phrase_accordingly() -> None:
    # The negation reads as negation; REFERENCES reads as a reference, never presence.
    assert SpatialLink.ABSENT.phrase == "not at"
    assert SpatialLink.REFERENCES.phrase == "referring to"
    # An assertion by a person reads as attribution, never as a weight or a doubt: the model
    # records WHO asserts, and leaves credibility to the court.
    assert Attestation.PERSON.phrase == "as stated by a person"
    # `cached` must not read as presence — it is the verb that leaves no spatial claim.
    assert EntityLink.CACHED.phrase == "cached"


# --- is_standard_part predicate -------------------------------------------

def test_is_standard_part_accepts_documented_names_rejects_arbitrary() -> None:
    for part in ("raw_position", "latitude", "longitude",
                 "horizontal_accuracy_m", "beam_azimuth_deg", "zone",
                 "lower", "upper", "duration", "lower_date", "upper_time"):
        assert is_standard_part(part)
    assert not is_standard_part("not_a_real_part")
    assert not is_standard_part("")


# --- column_cast: the implicit cast keys on assignable field names ---------

def test_column_cast_int_fires_for_assignable_integer_fields() -> None:
    # The implicit int-cast must fire for the integer fields a preset can actually MAP
    # (assignable names), mirroring how the float cast works — otherwise it is dead and a
    # preset would have to restate `cast: int`.
    for field in ("multiplicity", "time_accuracy_us_lower", "time_accuracy_us_upper"):
        assert column_cast(field) is int
    # A float-shaped source value (e.g. pandas materialising "2.0") coerces to the integer 2.
    cast = column_cast("multiplicity")
    assert cast is not None and cast(2.0) == 2


def test_column_cast_float_and_free_text_unchanged() -> None:
    # Float metrics still cast to float; a free-text field carries no implicit cast. The
    # datum-agnostic angle assignable is ``latitude`` (not the L2 column ``latitude_wgs84``).
    assert column_cast("latitude") is float
    assert column_cast("horizontal_accuracy_m") is float
    assert column_cast("source_label") is None
    # The downstream write-column names are NOT the assignable keys, so they do not match
    # (the previous dead-branch bug keyed on these and never fired).
    assert column_cast("lower_accuracy_us") is None
    assert column_cast("record_rank") is None
