"""Qt-free unit tests for gui/spatiotemporal_query.py.

Covers:     ``split_identifiers`` (commas/newlines/blanks), ``duration_from`` (unit
            scaling + unknown-unit/negative rejection), and ``build_request`` (inputs ->
            ``ConcomitanceRequest``: both groups' criteria/mode, the case_2 None-vs-blank
            handling, and the threshold/window mapping).
Uses:       gui.spatiotemporal_query and model_atlas.analyze.concomitance_request shapes.
No Qt import — these are pure functions.
"""
from __future__ import annotations

import datetime as dt

import pytest

from gui.spatiotemporal_query import build_request, duration_from, split_identifiers
from model_atlas.analyze.concomitance_request import Criterion, CriterionKind, MatchMode


# --- split_identifiers -----------------------------------------------------------------

def test_split_identifiers_handles_commas_newlines_and_blanks() -> None:
    assert split_identifiers("a, b\nc,,  \n d ") == ("a", "b", "c", "d")
    assert split_identifiers("   ") == ()


# --- duration_from -----------------------------------------------------------------------

def test_duration_from_scales_by_unit() -> None:
    assert duration_from(10, "minutes") == dt.timedelta(minutes=10)
    assert duration_from(2.5, "hours") == dt.timedelta(hours=2, minutes=30)
    assert duration_from(0, "seconds") == dt.timedelta(0)


def test_duration_from_rejects_unknown_unit_and_negative() -> None:
    with pytest.raises(ValueError):
        duration_from(1, "fortnights")
    with pytest.raises(ValueError):
        duration_from(-1, "minutes")


# --- build_request -----------------------------------------------------------------------

def test_build_request_maps_both_groups_and_thresholds() -> None:
    g1 = (Criterion(CriterionKind.IDENTIFIER, "+41791234567"),)
    g2 = (Criterion(CriterionKind.ENTITY_TYPE, "event"),)
    request = build_request(
        case_1="/case/one.matlas.sqlite",
        case_2=None,
        group_1_criteria=g1, group_1_mode=MatchMode.ANY,
        group_2_criteria=g2, group_2_mode=MatchMode.ALL,
        space_threshold_m=100.0,
        time_before_value=10, time_before_unit="minutes",
        time_after_value=30, time_after_unit="minutes",
        use_accuracy=True,
        output_path="/out/concomitance.csv",
    )
    assert request.case_1.name == "one.matlas.sqlite"
    assert request.case_2 is None
    assert request.group_1.criteria == g1
    assert request.group_1.mode is MatchMode.ANY
    assert request.group_2.criteria == g2
    assert request.group_2.mode is MatchMode.ALL
    assert request.space_threshold_m == 100.0
    assert request.time_before == dt.timedelta(minutes=10)
    assert request.time_after == dt.timedelta(minutes=30)
    assert request.use_accuracy is True
    assert request.output_path.name == "concomitance.csv"


def test_build_request_blank_case_2_is_none() -> None:
    request = build_request(
        case_1="/case/one.matlas.sqlite", case_2="   ",
        group_1_criteria=(), group_1_mode=MatchMode.ANY,
        group_2_criteria=(), group_2_mode=MatchMode.ANY,
        space_threshold_m=1.0,
        time_before_value=0, time_before_unit="seconds",
        time_after_value=0, time_after_unit="seconds",
        use_accuracy=False, output_path="/out/x.csv")
    assert request.case_2 is None


def test_build_request_carries_a_real_second_case() -> None:
    request = build_request(
        case_1="/case/one.matlas.sqlite", case_2="/case/two.matlas.sqlite",
        group_1_criteria=(), group_1_mode=MatchMode.ANY,
        group_2_criteria=(), group_2_mode=MatchMode.ANY,
        space_threshold_m=1.0,
        time_before_value=0, time_before_unit="seconds",
        time_after_value=0, time_after_unit="seconds",
        use_accuracy=False, output_path="/out/x.csv")
    assert request.case_2 is not None
    assert request.case_2.name == "two.matlas.sqlite"
