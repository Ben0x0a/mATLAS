"""Tests for the observation verbaliser (``model_atlas.core.verbalize``).

Defines:    the golden-sentence tests against the worked-example demo store (the
            worked examples render to sensible sentences, byte-for-byte), the semantic
            guards (negation reads as explicit negation, ``references`` never
            reads as presence, instant renders "at" vs interval "from ... to
            ..."), the per-vocabulary distinct-rendering test (no two members of
            a phrased vocabulary produce identical sentences on the same row
            skeleton), and the no-invented-facts tests (a positionless observation
            says the absence, ``concerns`` omitted when NULL, a typed mention
            falls back to the entity type).
Used by:    the verification gate (pytest tests/core/test_verbalize.py).
Depends on: ``model_atlas.core.verbalize`` (under test),
            ``model_atlas.core.flat_view`` (the flat rows the verbaliser
            consumes), ``model_atlas.core.vocabularies`` (the phrase fragments),
            pandas, and the worked-example demo DB
            ``docs/examples/matlas_example.sqlite`` (28 observations).

Note on ``TemporalKind``: its shape is not a projection column, so the verbaliser
derives instant vs interval from the bounds themselves; the INSTANT/INTERVAL
members are exercised through those two shapes. The OPEN_LOWER/OPEN_UPPER
fragments are not row-drivable (a lone bound renders as an instant per the D37
brief), and INTERVAL and OPEN_UPPER deliberately share the fragment "from" — so
the member-distinctness test covers the four row-driven vocabularies.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from model_atlas.core import flat_view
from model_atlas.core import verbalize
from model_atlas.core import vocabularies as V

_DEMO_DB = Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"

# A full observation-row skeleton (every corner populated, interval timespan): the
# distinct-rendering tests vary ONE vocabulary column at a time on this base.
_SKELETON: dict[str, Any] = {
    "entity": "+41790000000",
    "entity_type": "device",
    "concerns": None,
    "activity_role": "unknown",
    # A bearer is present so the ENTITY-corner verb has an object to render against; the
    # `self` member deliberately suppresses the clause, which is itself part of the shape.
    "entity_link": "served_by",
    "bearer_entity": "228-01-2042-70110",
    "spatial_link": "at",
    "attestation": "system_record",
    "occupancy": "continuous",
    "latitude_wgs84": 46.2,
    "longitude_wgs84": 6.15,
    "raw_position": "Somewhere",
    "time_lower_datetime_iso": "2026-03-14T02:00:00+01:00",
    "time_upper_datetime_iso": "2026-03-14T03:00:00+01:00",
}


@pytest.fixture(scope="module")
def demo() -> sqlite3.Connection:
    # mode=ro: the verbaliser is a read-only consumer; the fixture cannot mutate.
    connection = sqlite3.connect(f"file:{_DEMO_DB}?mode=ro", uri=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def frame(demo: sqlite3.Connection) -> pd.DataFrame:
    return flat_view.build_flat_view(demo, include_source_columns=False)


def _one_row(frame: pd.DataFrame, mask: pd.Series) -> pd.Series:
    rows = frame[mask]
    assert len(rows) >= 1
    return rows.iloc[0]


# --- golden sentences against the T00 demo store ------------------------------

def test_golden_instant_gps_fix_typed_mention(frame: pd.DataFrame) -> None:
    # A purely typed mention (NULL identifier) falls back to the type; equal
    # bounds render as an instant; the assumed-UTC " N/A" sentinel stays visible.
    row = frame.iloc[0]
    assert verbalize.verbalize(row) == (
        "The device (an entity) was at 46.193, 6.129, "
        "once during 2026-03-13T19:00:00 N/A, as measured by an instrument."
    )


def test_golden_cdr_interval_cell_ref(frame: pd.DataFrame) -> None:
    # Place = the raw cell ref plus the register's coordinates (the method's named
    # substitution); two distinct bounds render as "the interval from ... to ...".
    row = _one_row(frame, (frame["source_label"] == "CDR op-A")
                   & (frame["time_lower_datetime_iso"] == "2026-03-13T18:40:12+01:00"))
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 (the caller) was served by 228-01-2042-70110, "
        "near 228-01-2042-70110, continuously during the interval from "
        "2026-03-13T18:40:12+01:00 to 2026-03-13T18:45:24+01:00, "
        "as recorded by a system."
    )


def test_golden_concerns_clause(frame: pd.DataFrame) -> None:
    # ``concerns`` present -> the "(referring ...)" clause renders; a claim reads
    # as a claim (claimed_at / reported_for fragments).
    row = _one_row(frame, frame["preset_id"] == "police_reg")
    assert verbalize.verbalize(row) == (
        "Entity GE-2026-04412 (referring GE-2026-04412; an entity) was at "
        "Rue du Stand 15, Geneve, continuously during the interval from "
        "2026-03-14T02:30:00+01:00 to 2026-03-14T02:45:00+01:00, "
        "as recorded by a system."
    )


def test_golden_references_pin(frame: pd.DataFrame) -> None:
    # A shared chat pin: the place is REFERRED TO, never a presence.
    row = _one_row(frame, frame["source_label"] == "chat pin")
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 (the sender) shared scene-pin, "
        "referring to 46.203, 6.14, once during 2026-03-14T01:11:20 N/A, "
        "as recorded by a system."
    )


def test_golden_unintegrated_place_falls_back_to_raw(frame: pd.DataFrame) -> None:
    # The nav "Home" leg has a spatial expression but no integration: the place is
    # the verbatim raw alone — never invented coordinates.
    row = _one_row(frame, (frame["preset_id"] == "nav_generic")
                   & (frame["raw_position"] == "Home"))
    assert verbalize.verbalize(row) == (
        "Entity jdoe@gmail.com (an entity) searched for Home, "
        "referring to Home, once during 2026-03-13T21:04:09+00:00, "
        "as recorded by a system."
    )


def test_golden_alibi_negation(frame: pd.DataFrame) -> None:
    row = _one_row(frame, frame["source_label"] == "alibi")
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 (an entity) was not at Home, "
        "continuously during the interval from 2026-03-14T02:30:00+01:00 "
        "to 2026-03-14T02:45:00+01:00, as stated by a person."
    )


def test_all_demo_rows_render_deterministically(frame: pd.DataFrame) -> None:
    # Every one of the 28 observations yields one non-empty full-stop sentence, and
    # a second call over the same row is byte-for-byte identical; a plain-dict
    # copy of the row must render the same sentence as the Series (both accepted).
    assert len(frame) == 28
    for _, row in frame.iterrows():
        sentence = verbalize.verbalize(row)
        assert sentence and sentence.endswith(".")
        assert verbalize.verbalize(row) == sentence
        assert verbalize.verbalize(row.to_dict()) == sentence


# --- semantic guards -----------------------------------------------------------

def test_negation_reads_as_explicit_negation(frame: pd.DataFrame) -> None:
    row = _one_row(frame, frame["source_label"] == "alibi")
    sentence = verbalize.verbalize(row)
    assert "was not at" in sentence


def test_references_never_reads_as_presence(frame: pd.DataFrame) -> None:
    for _, row in frame[frame["spatial_link"] == "references"].iterrows():
        sentence = verbalize.verbalize(row)
        assert "referring to" in sentence
        # No presence wording may leak in: the pin points at the place.
        assert "was at" not in sentence
        assert "was present" not in sentence


def test_instant_renders_at_interval_renders_from_to() -> None:
    instant = dict(_SKELETON,
                   time_upper_datetime_iso=_SKELETON["time_lower_datetime_iso"])
    assert verbalize.verbalize(instant).endswith(
        ", continuously during 2026-03-14T02:00:00+01:00, as recorded by a system.")
    interval = dict(_SKELETON)
    assert ("the interval from 2026-03-14T02:00:00+01:00 to "
            "2026-03-14T03:00:00+01:00" in verbalize.verbalize(interval))


def test_lone_bound_renders_as_instant() -> None:
    lone = dict(_SKELETON, time_upper_datetime_iso=None)
    sentence = verbalize.verbalize(lone)
    assert sentence.endswith(", continuously during 2026-03-14T02:00:00+01:00, as recorded by a system.")
    assert "interval" not in sentence


def test_claim_reads_as_claim() -> None:
    claim = dict(_SKELETON, attestation="person")
    assert "as stated by a person" in verbalize.verbalize(claim)


# --- every phrased member renders distinctly ------------------------------------

@pytest.mark.parametrize("column, vocabulary", [
    ("entity_link", V.EntityLink),
    ("spatial_link", V.SpatialLink),
    ("occupancy", V.Occupancy),
    ("attestation", V.Attestation),
    ("activity_role", V.ActivityRole),
])
def test_every_member_renders_distinctly(column: str, vocabulary: type) -> None:
    # No two members of a row-driven vocabulary may produce identical sentences on
    # the same skeleton — a collision would make two different claims read alike.
    sentences = {member: verbalize.verbalize(dict(_SKELETON, **{column: member.value}))
                 for member in vocabulary}
    assert len(set(sentences.values())) == len(vocabulary)
    # The fragments themselves are non-empty and unique within the vocabulary.
    phrases = [member.phrase for member in vocabulary]
    assert all(phrases)
    assert len(set(phrases)) == len(phrases)


def test_unknown_members_degrade_gracefully() -> None:
    row = dict(_SKELETON,
               spatial_link="unknown",
               attestation="unknown",
               occupancy="unknown",
               activity_role="unknown")
    sentence = verbalize.verbalize(row)
    # The unknown fragments STATE the uncertainty; nothing crashes, nothing invents, and
    # an undetermined relation never quietly reads as presence.
    assert "in an undetermined relation to" in sentence
    assert "at an undetermined point during" in sentence
    assert "as attested by an unknown party" in sentence
    assert "(an entity)" in sentence


# --- no invented facts -----------------------------------------------------------

def test_positionless_observation_says_the_absence() -> None:
    row = dict(_SKELETON, latitude_wgs84=None, longitude_wgs84=None,
               raw_position=None)
    assert verbalize.verbalize(row) == (
        "Entity +41790000000 (an entity) was served by 228-01-2042-70110, "
        "with no recorded position, continuously during the interval from "
        "2026-03-14T02:00:00+01:00 to 2026-03-14T03:00:00+01:00, "
        "as recorded by a system."
    )


def test_timeless_observation_says_the_absence() -> None:
    row = dict(_SKELETON, time_lower_datetime_iso=None,
               time_upper_datetime_iso=None)
    assert verbalize.verbalize(row) == (
        "Entity +41790000000 (an entity) was served by 228-01-2042-70110, "
        "at Somewhere (46.2, 6.15), "
        "with no recorded time, as recorded by a system."
    )


def test_both_corners_empty_still_renders() -> None:
    row = {"entity": "+41790000000", "entity_type": "device"}
    assert verbalize.verbalize(row) == (
        "Entity +41790000000, with no recorded position and with no recorded time."
    )


def test_concerns_clause_omitted_when_null() -> None:
    assert "referring" not in verbalize.verbalize(dict(_SKELETON))
    with_concerns = dict(_SKELETON, concerns="Marie Dupont")
    assert "(referring Marie Dupont; an entity)" in verbalize.verbalize(with_concerns)


def test_typed_mention_falls_back_to_entity_type() -> None:
    typed = dict(_SKELETON, entity=None)
    assert verbalize.verbalize(typed).startswith("The device (an entity)")
    nameless = dict(_SKELETON, entity=None, entity_type=None)
    assert verbalize.verbalize(nameless).startswith("An unknown entity")


def test_unlinked_position_states_the_record_not_a_presence() -> None:
    # An E-S link of NULL must not borrow any presence wording: the neutral glue
    # states that a position was recorded, nothing more.
    row = dict(_SKELETON, spatial_link=None)
    sentence = verbalize.verbalize(row)
    assert "had a recorded position of Somewhere (46.2, 6.15)" in sentence
    assert "was at" not in sentence
