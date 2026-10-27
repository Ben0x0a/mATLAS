"""Tests for the observation verbaliser (``model_atlas.core.verbalize``).

Defines:    the golden-sentence tests against the worked-example demo store (the
            worked examples render to sensible sentences, byte-for-byte), the semantic
            guards (negation reads as explicit negation, ``searched`` / ``shared``
            never read as presence, instant renders "at" vs interval "from ... to
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
    # A `party` row: the antenna holds the position, so the spatial verb takes the bearer as
    # its object and the place follows. `subject` makes the place the object instead.
    "entity_link_matlas": "party",
    "bearer_entity": "228-01-2042-70110",
    "spatial_link_matlas": "served",
    "temporal_link_matlas": "at_least_once",
    "latitude_wgs84": 46.2,
    "longitude_wgs84": 6.15,
    "position_raw": "place:Somewhere",
    # 02:00 and 03:00 in a +01:00 zone, as canonical UTC µs + the offset an analyst adds
    # to read them locally — the two columns the verbaliser renders from.
    "time_lower_unix_utc_us": 1_773_450_000_000_000,
    "time_upper_unix_utc_us": 1_773_453_600_000_000,
    "offset_to_local_hours_lower": 1.0,
    "offset_to_local_hours_upper": 1.0,
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
        "The device was measured at 46.193, 6.129, at 2026-03-13T20:00:00.000000+01:00."
    )


def test_golden_cdr_interval_cell_ref(frame: pd.DataFrame) -> None:
    # Place = the raw cell ref plus the register's coordinates (the method's named
    # substitution); two distinct bounds render as "the interval from ... to ...".
    row = _one_row(frame, (frame["source_label_matlas"] == "CDR op-A")
                   & (frame["time_lower_unix_utc_us"] == 1_773_423_612_000_000))
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 was served by 228-01-2042-70110, "
        "with no recorded position, at least once during the interval from "
        "2026-03-13T18:40:12.000000+01:00 to 2026-03-13T18:45:24.000000+01:00."
    )


def test_golden_concerns_clause(frame: pd.DataFrame) -> None:
    # ``concerns`` LEFT the projection, so a flat-view row can no longer produce the
    # "(referring ...)" clause. The clause itself survives for any caller that supplies the
    # column, which is why the second half asserts it still renders when it is present.
    row = _one_row(frame, frame["preset_id_matlas"] == "police_reg")
    assert verbalize.verbalize(row) == (
        "Entity GE-2026-04412, with no recorded position, throughout the interval from "
        "2026-03-14T02:30:00.000000+01:00 to 2026-03-14T02:45:00.000000+01:00."
    )
    assert "(referring A. Moreau" in verbalize.verbalize(
        dict(row, concerns="A. Moreau"))


def test_golden_shared_pin(frame: pd.DataFrame) -> None:
    # A shared chat pin: the pin holds the position and the phone only shared it — the verb
    # carries that, so no presence wording appears.
    row = _one_row(frame, frame["source_label_matlas"] == "chat pin")
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 shared scene-pin, at 46.203, 6.14, "
        "at 2026-03-14T02:11:20.000000+01:00."
    )


def test_unintegrated_place_says_the_absence(frame: pd.DataFrame) -> None:
    # The nav "Home" leg has a spatial expression but NO integration. Since position_raw
    # now records only what produced latitude_wgs84/longitude_wgs84, such a place reaches
    # the flat row with nothing to say — and the sentence SAYS that rather than inventing
    # a place or dropping the line.
    row = _one_row(frame, (frame["preset_id_matlas"] == "nav_generic")
                   & frame["latitude_wgs84"].isna())
    sentence = verbalize.verbalize(row)
    assert "with no recorded position" in sentence
    assert "searched for" in sentence


def test_golden_alibi_negation(frame: pd.DataFrame) -> None:
    row = _one_row(frame, frame["source_label_matlas"] == "alibi")
    assert verbalize.verbalize(row) == (
        "Entity +41791234567 was located at Home, with no recorded position, "
        "never during the interval from 2026-03-14T02:30:00.000000+01:00 "
        "to 2026-03-14T02:45:00.000000+01:00."
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

def test_negation_reads_as_explicit_negation() -> None:
    never = dict(_SKELETON, temporal_link_matlas="never")
    assert ", never during the interval from" in verbalize.verbalize(never)
    instant = dict(never, time_upper_unix_utc_us=_SKELETON["time_lower_unix_utc_us"])
    assert verbalize.verbalize(instant).endswith(", not at 2026-03-14T02:00:00.000000+01:00.")
    left = dict(_SKELETON, temporal_link_matlas="not_throughout")
    assert ", not throughout the interval from" in verbalize.verbalize(left)


def test_non_contact_verbs_never_read_as_presence() -> None:
    for verb, fragment in (("searched", "searched for"), ("shared", "shared"),
                           ("cached", "cached")):
        sentence = verbalize.verbalize(dict(_SKELETON, spatial_link_matlas=verb))
        assert f"Entity +41790000000 {fragment} 228-01-2042-70110" in sentence
        assert "was at" not in sentence and "was located" not in sentence


def test_subject_takes_the_place_as_object() -> None:
    # The bearer of a `subject` row IS the entity: naming it again would read "X ... X".
    row = dict(_SKELETON, entity_link_matlas="subject", bearer_entity="+41790000000",
               spatial_link_matlas="measured")
    assert verbalize.verbalize(row).startswith("Entity +41790000000 was measured at 46.2, 6.15, ")


def test_unidentified_bearer_is_named_by_its_type() -> None:
    row = dict(_SKELETON, bearer_entity=None, bearer_entity_type="antenna")
    assert "was served by an unidentified antenna, at 46.2, 6.15" in verbalize.verbalize(row)


def test_instant_renders_at_interval_renders_from_to() -> None:
    instant = dict(_SKELETON,
                   time_upper_unix_utc_us=_SKELETON["time_lower_unix_utc_us"])
    assert verbalize.verbalize(instant).endswith(", at 2026-03-14T02:00:00.000000+01:00.")
    interval = dict(_SKELETON)
    assert (", at least once during the interval from 2026-03-14T02:00:00.000000+01:00 to "
            "2026-03-14T03:00:00.000000+01:00." in verbalize.verbalize(interval))


def test_lone_bound_renders_as_instant() -> None:
    lone = dict(_SKELETON, time_upper_unix_utc_us=None)
    sentence = verbalize.verbalize(lone)
    assert sentence.endswith(", at 2026-03-14T02:00:00.000000+01:00.")
    assert "interval" not in sentence


# --- every phrased member renders distinctly ------------------------------------

@pytest.mark.parametrize("column, vocabulary", [
    ("entity_link_matlas", V.EntityLink),
    ("spatial_link_matlas", V.SpatialLink),
    ("temporal_link_matlas", V.TemporalLink),
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
               entity_link_matlas="unknown",
               bearer_entity=None,
               spatial_link_matlas="unknown",
               temporal_link_matlas="unknown")
    # The unknown fragments STATE the uncertainty; nothing crashes, nothing invents, and
    # an undetermined relation never quietly reads as presence.
    assert verbalize.verbalize(row) == (
        "Entity +41790000000 (in an undetermined role) was linked to 46.2, 6.15, "
        "for an undetermined part of the interval from 2026-03-14T02:00:00.000000+01:00 "
        "to 2026-03-14T03:00:00.000000+01:00."
    )


# --- no invented facts -----------------------------------------------------------

def test_positionless_observation_says_the_absence() -> None:
    row = dict(_SKELETON, latitude_wgs84=None, longitude_wgs84=None,
               position_raw=None)
    # Decision 55: no geometry means no spatial link, so the bearer is introduced by the
    # entity-link phrase rather than a spatial verb.
    row["spatial_link_matlas"] = None
    assert verbalize.verbalize(row) == (
        "Entity +41790000000, in relation with 228-01-2042-70110, "
        "with no recorded position, at least once during the interval from "
        "2026-03-14T02:00:00.000000+01:00 to 2026-03-14T03:00:00.000000+01:00."
    )


def test_timeless_observation_says_the_absence() -> None:
    row = dict(_SKELETON, time_lower_unix_utc_us=None,
               time_upper_unix_utc_us=None)
    assert verbalize.verbalize(row) == (
        "Entity +41790000000 was served by 228-01-2042-70110, "
        "at 46.2, 6.15, with no recorded time."
    )


def test_both_corners_empty_still_renders() -> None:
    row = {"entity": "+41790000000", "entity_type": "device"}
    assert verbalize.verbalize(row) == (
        "Entity +41790000000, with no recorded position and with no recorded time."
    )


def test_concerns_clause_omitted_when_null() -> None:
    assert "referring" not in verbalize.verbalize(dict(_SKELETON))
    with_concerns = dict(_SKELETON, concerns="Marie Dupont")
    assert "(referring Marie Dupont)" in verbalize.verbalize(with_concerns)


def test_typed_mention_falls_back_to_entity_type() -> None:
    typed = dict(_SKELETON, entity=None)
    assert verbalize.verbalize(typed).startswith("The device")
    nameless = dict(_SKELETON, entity=None, entity_type=None)
    assert verbalize.verbalize(nameless).startswith("An unknown entity")


def test_unlinked_position_states_the_record_not_a_presence() -> None:
    # A spatial link of NULL must not borrow any presence wording: the neutral glue
    # states that a position was recorded, nothing more.
    row = dict(_SKELETON, spatial_link_matlas=None)
    sentence = verbalize.verbalize(row)
    assert "had a recorded position of 46.2, 6.15" in sentence
    assert "was at" not in sentence
