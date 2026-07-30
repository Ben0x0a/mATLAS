"""Unit tests for the core run-DB vocabularies and family declarations.

Defines:    a round-trip test proving every enum-valued string in the worked-example
            demo DB is a member of the corresponding enum (columns enumerated
            dynamically, no hardcoded row lists), the Locates/Genesis derivation tests,
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
    EntityPositionLink,
    EntityRole,
    EntityTimeLink,
    EntityType,
    Genesis,
    Locates,
    Origin,
    RecordState,
    SourceTier,
    SpatialReferential,
    SpatialRole,
    SpatialTemporalLink,
    TemporalKind,
    TemporalReferential,
    TemporalRole,
    column_cast,
    is_standard_part,
)

# The T00 demo DB (read-only): every enum-valued string it stores must round-trip.
_DEMO_DB = (
    Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"
)

# (table, column) -> the enum whose members must contain every stored value. The
# columns are the enum-valued (closed-vocabulary) ones; free-text/method-defined columns
# (e.g. analysis.record_type, activity.context_json, entity.identifier_type — a free string
# whose known kinds IdentifierType only suggests) are deliberately excluded.
_ENUM_COLUMNS: dict[tuple[str, str], type[Enum]] = {
    ("entity", "type"): EntityType,
    ("entity", "concerns_origin"): Origin,
    ("assertion", "entity_origin"): Origin,
    ("assertion", "entity_role"): EntityRole,
    ("assertion", "entity_position_link"): EntityPositionLink,
    ("assertion", "entity_time_link"): EntityTimeLink,
    ("assertion", "spatial_temporal_link"): SpatialTemporalLink,
    ("assertion", "deleted"): RecordState,
    ("spatial_expression", "referential"): SpatialReferential,
    ("spatial_component", "spatial_role"): SpatialRole,
    ("temporal_expression", "kind"): TemporalKind,
    ("temporal_expression", "referential"): TemporalReferential,
    ("temporal_component", "temporal_role"): TemporalRole,
    ("activity", "kind"): ActivityKind,
    ("raw_record", "recovery_state"): RecordState,
    ("edit_log", "edit_kind"): EditKind,
    ("source", "source_tier"): SourceTier,
}


def _distinct_values(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    # HOW: read the distinct non-NULL values actually present, so the fixture drives
    # the assertion rather than a hardcoded list that could drift from the DB.
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


# --- Locates derivation (decision 11) -------------------------------------

def test_locates_derivation_matches_decision_11() -> None:
    expected = {
        EntityPositionLink.AT: Locates.SELF,
        EntityPositionLink.CLAIMED_AT: Locates.SELF,
        EntityPositionLink.WITHIN_RANGE_OF: Locates.INFRASTRUCTURE,
        EntityPositionLink.AT_FIXED_DETECTOR: Locates.INFRASTRUCTURE,
        EntityPositionLink.REFERENCES: Locates.REFERENCED,
        EntityPositionLink.INFERRED: Locates.UNKNOWN,
        EntityPositionLink.UNKNOWN: Locates.UNKNOWN,
    }
    # Every member must resolve, so a future addition cannot silently lack a locates.
    assert {link: link.locates for link in EntityPositionLink} == expected


def test_genesis_derivation_covers_every_member() -> None:
    # Every E-T member (including the negation) must map to a genesis.
    assert {link: link.genesis for link in EntityTimeLink} == {
        EntityTimeLink.OBSERVED_AT: Genesis.PRESENCE,
        EntityTimeLink.EVENT_AT: Genesis.ACTION,
        EntityTimeLink.RECORDED_AT: Genesis.SYSTEM,
        EntityTimeLink.REPORTED_FOR: Genesis.REPORTED,
        EntityTimeLink.INTENDED_FOR: Genesis.PLANNED,
        EntityTimeLink.NEVER_DURING_INTERVAL: Genesis.NEGATED,
        EntityTimeLink.UNKNOWN: Genesis.UNKNOWN,
    }


# --- Phrase fragments (decision 37) ---------------------------------------

_PHRASED_ENUMS = (
    EntityPositionLink,
    EntityTimeLink,
    SpatialTemporalLink,
    EntityRole,
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
    assert EntityTimeLink.NEVER_DURING_INTERVAL.phrase == "was never present during"
    assert SpatialTemporalLink.NEVER_DURING_INTERVAL.phrase == "at no point during"
    assert EntityPositionLink.REFERENCES.phrase == "referred to"
    # A claim reads as a claim, not a measured presence.
    assert EntityPositionLink.CLAIMED_AT.phrase == "was claimed to be at"


# --- is_standard_part predicate -------------------------------------------

def test_is_standard_part_accepts_documented_names_rejects_arbitrary() -> None:
    for part in ("raw_position", "latitude_wgs84", "longitude_wgs84",
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
    # Float metrics still cast to float; a free-text field carries no implicit cast.
    assert column_cast("latitude_wgs84") is float
    assert column_cast("horizontal_accuracy_m") is float
    assert column_cast("source_label") is None
    # The downstream write-column names are NOT the assignable keys, so they do not match
    # (the previous dead-branch bug keyed on these and never fired).
    assert column_cast("lower_accuracy_us") is None
    assert column_cast("record_rank") is None
