"""Qt-free unit tests for the two spatiotemporal mapping seams.

Covers:     the input->query-params mapping (``build_query`` + ``split_identifiers`` /
            ``duration_from``: axes AND-ed, empties left unconstrained, unknown unit
            rejected) and the result->table-row mapping (``result_to_rows`` +
            the ``format_*`` cell formatters: identity/unidentified, distance, and the
            overlap-vs-gap wording). No Qt import — these are pure functions.
Uses:       gui.spatiotemporal_query and model_atlas.analyze.concomitance shapes.
"""
from __future__ import annotations

import datetime as dt

import pytest

from gui.spatiotemporal_query import (
    RESULT_COLUMNS,
    build_query,
    duration_from,
    format_distance_m,
    format_entity,
    format_time_gap_us,
    result_to_rows,
    split_identifiers,
)
from model_atlas.analyze.concomitance import (
    ConcomitantPair,
    EntityRef,
    ResultSet,
)


# --- input -> query params -------------------------------------------------------

def test_split_identifiers_handles_commas_newlines_and_blanks() -> None:
    assert split_identifiers("a, b\nc,,  \n d ") == ("a", "b", "c", "d")
    assert split_identifiers("   ") == ()


def test_duration_from_scales_by_unit() -> None:
    assert duration_from(10, "minutes") == dt.timedelta(minutes=10)
    assert duration_from(2.5, "hours") == dt.timedelta(hours=2, minutes=30)
    assert duration_from(0, "seconds") == dt.timedelta(0)


def test_duration_from_rejects_unknown_unit_and_negative() -> None:
    with pytest.raises(ValueError):
        duration_from(1, "fortnights")
    with pytest.raises(ValueError):
        duration_from(-1, "minutes")


def test_build_query_maps_inputs_to_selector_and_kwargs() -> None:
    query = build_query(
        types=["vehicle", "device"],
        identifier_types=["plate"],
        identifiers_text="GE123456, +41791234567",
        space_threshold_m=150.0,
        time_value=5,
        time_unit="minutes",
    )
    selector = query.entity_selector
    assert tuple(selector.types) == ("vehicle", "device")
    assert tuple(selector.identifier_types) == ("plate",)
    assert tuple(selector.identifiers) == ("GE123456", "+41791234567")
    assert query.space_threshold_m == 150.0
    assert query.time_threshold == dt.timedelta(minutes=5)
    # kwargs() feeds find_concomitant directly.
    kwargs = query.kwargs()
    assert kwargs["entity_selector"] is selector
    assert kwargs["space_threshold_m"] == 150.0
    assert kwargs["time_threshold"] == dt.timedelta(minutes=5)


def test_build_query_leaves_empty_axes_unconstrained() -> None:
    query = build_query(
        types=[], identifier_types=[], identifiers_text="",
        space_threshold_m=50.0, time_value=1, time_unit="hours")
    selector = query.entity_selector
    assert tuple(selector.types) == ()
    assert tuple(selector.identifier_types) == ()
    assert tuple(selector.identifiers) == ()


# --- cell formatters -------------------------------------------------------------

def test_format_entity_shows_identity_or_unidentified_marker() -> None:
    identified = EntityRef(entity_id="e1", identifier="+41791234567",
                           identifier_type="msisdn", type="device")
    assert format_entity(identified) == "+41791234567 (msisdn/device)"
    unidentified = EntityRef(entity_id="e2", identifier=None,
                             identifier_type="none", type="device")
    # A NULL identifier reads as an em dash, never a blank cell.
    assert format_entity(unidentified) == "— (none/device)"


def test_format_distance_and_time_gap() -> None:
    assert format_distance_m(47.06) == "47.1"
    assert format_time_gap_us(0) == "0s (overlap)"
    assert format_time_gap_us(90_000_000) == "1m 30s"
    assert format_time_gap_us(3_661_000_000) == "1h 1m 1s"
    assert format_time_gap_us(1_500_000) == "1.500s"


# --- ResultSet -> rows -----------------------------------------------------------

def _pair(assertion_a: str, assertion_b: str) -> ConcomitantPair:
    return ConcomitantPair(
        entity_a=EntityRef("ea", "+41790000001", "msisdn", "device"),
        entity_b=EntityRef("eb", "GE123456", "plate", "vehicle"),
        distance_m=12.34, time_gap_us=0,
        assertion_a=assertion_a, assertion_b=assertion_b)


def test_result_to_rows_maps_each_pair_in_column_order() -> None:
    result = ResultSet(
        pairs=(_pair("a1", "a2"),),
        space_threshold_m=50.0, time_threshold=dt.timedelta(minutes=10),
        candidate_count=2)
    rows = result_to_rows(result)
    assert len(rows) == 1
    cells = rows[0].as_cells()
    assert len(cells) == len(RESULT_COLUMNS)
    assert cells == (
        "+41790000001 (msisdn/device)", "GE123456 (plate/vehicle)",
        "12.3", "0s (overlap)", "a1", "a2")


def test_result_to_rows_empty_result_is_empty_list() -> None:
    result = ResultSet(pairs=(), space_threshold_m=50.0,
                       time_threshold=dt.timedelta(minutes=10), candidate_count=0)
    assert result_to_rows(result) == []
