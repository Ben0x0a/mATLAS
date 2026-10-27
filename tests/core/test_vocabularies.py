"""Unit tests for the core run-DB vocabularies and family declarations.

Defines:    a round-trip test proving every enum-valued string in the worked-example
            demo DB is a member of the corresponding enum (columns enumerated
            dynamically, no hardcoded row lists), the decision-55 link invariants,
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
    EntityLink,
    EntityType,
    TemporalLink,
    Origin,
    RecordState,
    RecoverySource,
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
# (e.g. entity.identifier_type — a free string
# whose known kinds IdentifierType only suggests) are deliberately excluded.
_ENUM_COLUMNS: dict[tuple[str, str], type[Enum]] = {
    ("entity", "type"): EntityType,
    ("entity", "concerns_origin"): Origin,
    ("observation", "entity_link"): EntityLink,
    ("observation", "spatial_link"): SpatialLink,
    ("observation", "temporal_link"): TemporalLink,
    ("observation", "recovery_state"): RecordState,
    ("observation", "recovery_source"): RecoverySource,
    ("spatial_expression", "referential"): SpatialReferential,
    ("temporal_expression", "kind"): TemporalKind,
    ("temporal_expression", "referential"): TemporalReferential,
    ("raw_record", "recovery_state"): RecordState,
    ("edit_log", "edit_kind"): EditKind,
    ("source_file", "source_tier"): SourceTier,
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


# --- The links (decision 55) ----------------------------------------------

def test_negation_lives_only_on_the_temporal_link() -> None:
    """NOT-diamond-P == box-NOT-P, so negation storable on two arms would let one fact be
    written two ways — the real bug ``NEVER_DURING_INTERVAL`` caused on two enums at once.
    Only ``TemporalLink`` may carry a negative member."""
    negative_words = ("absent", "never", "not")
    for enum in (EntityLink, SpatialLink):
        assert not any(word in m.value for m in enum for word in negative_words), enum
    assert {TemporalLink.NEVER, TemporalLink.NOT_THROUGHOUT} == {
        m for m in TemporalLink if any(word in m.value for word in negative_words)}


def test_temporal_link_is_the_square_of_opposition_plus_unknown() -> None:
    assert [m.value for m in TemporalLink] == [
        "throughout", "at_least_once", "never", "not_throughout", "unknown"]


def test_instant_canonicalisation_keeps_one_encoding_per_fact() -> None:
    """On an instant box == diamond: each fact must map to ONE stored member."""
    assert {m: m.canonical_on_instant for m in TemporalLink} == {
        TemporalLink.THROUGHOUT: TemporalLink.AT_LEAST_ONCE,
        TemporalLink.AT_LEAST_ONCE: TemporalLink.AT_LEAST_ONCE,
        TemporalLink.NEVER: TemporalLink.NEVER,
        TemporalLink.NOT_THROUGHOUT: TemporalLink.NEVER,
        TemporalLink.UNKNOWN: TemporalLink.UNKNOWN,
    }


def test_entity_link_is_a_role_not_a_verb() -> None:
    assert [m.value for m in EntityLink] == ["subject", "party", "mentioned", "unknown"]


def test_no_link_has_an_instant_member() -> None:
    """An instant is the degenerate interval ``lower == upper``; its shape is
    ``TemporalKind``'s job, so no link may restate it."""
    for enum in (EntityLink, SpatialLink, TemporalLink):
        assert not any(m.value == "instant" for m in enum)
    assert any(m.value == "instant" for m in TemporalKind)


# --- Phrase fragments (decision 37) ---------------------------------------

_PHRASED_ENUMS = (
    EntityLink,
    SpatialLink,
    TemporalLink,
    TemporalKind,
)


@pytest.mark.parametrize("enum", _PHRASED_ENUMS)
def test_every_phrased_member_has_a_non_empty_phrase(enum: type[Enum]) -> None:
    for member in enum:
        assert isinstance(member.phrase, str) and member.phrase.strip(), (
            f"{enum.__name__}.{member.name} has an empty phrase"
        )


def test_negation_and_non_contact_members_phrase_accordingly() -> None:
    # The negation reads as negation, on an interval and on an instant alike.
    assert TemporalLink.NEVER.phrase == "never during"
    assert TemporalLink.NEVER.instant_phrase == "not at"
    assert TemporalLink.NOT_THROUGHOUT.instant_phrase == "not at"
    # `searched` / `cached` must not read as presence.
    assert SpatialLink.SEARCHED.phrase == "searched for"
    assert SpatialLink.CACHED.phrase == "cached"


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
    for field in ("time_accuracy_us_lower", "time_accuracy_us_upper"):
        assert column_cast(field) is int
    # A float-shaped source value (e.g. pandas materialising "2.0") coerces to the integer 2.
    cast = column_cast("time_accuracy_us_lower")
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
