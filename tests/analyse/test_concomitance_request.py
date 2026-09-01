"""Tests for model_atlas/analyse/concomitance/request.py (the Verify list).

Covers:     ``check_request``'s validation table (negative distance / negative window /
            both-zero / identical-groups WARNING that does NOT block / unwritable output
            folder) and ``same_selection``'s order-independent comparison.
Uses:       ``model_atlas.analyse.concomitance`` only — Qt-free, I/O-free apart
            from the output-folder writability probe.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from model_atlas.analyse.concomitance import (
    ConcomitanceRequest,
    Criterion,
    EntityGroup,
    MatchMode,
    Severity,
    check_request,
    same_selection,
)
from model_atlas.analyse.helpers import CriterionKind


def _request(tmp_path: Path, **overrides: object) -> ConcomitanceRequest:
    defaults: dict[str, object] = dict(
        case_1=tmp_path / "case.matlas.sqlite",
        case_2=None,
        group_1=EntityGroup(),
        group_2=EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),)),
        space_threshold_m=100.0,
        time_before=dt.timedelta(minutes=10),
        time_after=dt.timedelta(minutes=30),
        output_path=tmp_path / "out.csv",
    )
    defaults.update(overrides)
    return ConcomitanceRequest(**defaults)


def _messages(problems: tuple, severity: Severity) -> list[str]:
    return [p.message for p in problems if p.severity is severity]


# --- same_selection ------------------------------------------------------------------

def test_same_selection_is_order_independent() -> None:
    a = EntityGroup(criteria=(
        Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),
        Criterion(CriterionKind.IDENTIFIER_TYPE, "plate")))
    b = EntityGroup(criteria=(
        Criterion(CriterionKind.IDENTIFIER_TYPE, "plate"),
        Criterion(CriterionKind.ENTITY_TYPE, "vehicle")))
    assert same_selection(a, b)


def test_same_selection_false_on_different_mode_or_criteria() -> None:
    base = EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),))
    different_mode = EntityGroup(criteria=base.criteria, mode=MatchMode.ALL)
    different_criteria = EntityGroup(
        criteria=(Criterion(CriterionKind.ENTITY_TYPE, "device"),))
    assert not same_selection(base, different_mode)
    assert not same_selection(base, different_criteria)
    assert same_selection(EntityGroup(), EntityGroup())


# --- check_request: each row of the validation table ----------------------------------

def test_negative_distance_is_an_error(tmp_path: Path) -> None:
    problems = check_request(_request(tmp_path, space_threshold_m=-1.0))
    assert "Distance threshold must be 0 m or more." in _messages(problems, Severity.ERROR)


def test_negative_time_window_is_an_error(tmp_path: Path) -> None:
    before = check_request(_request(tmp_path, time_before=dt.timedelta(minutes=-1)))
    after = check_request(_request(tmp_path, time_after=dt.timedelta(minutes=-1)))
    for problems in (before, after):
        assert "Time windows must be 0 or more." in _messages(problems, Severity.ERROR)


def test_all_zero_thresholds_is_an_error(tmp_path: Path) -> None:
    problems = check_request(_request(
        tmp_path, space_threshold_m=0.0,
        time_before=dt.timedelta(0), time_after=dt.timedelta(0)))
    assert ("Set a distance or a time window; both zero matches nothing."
            in _messages(problems, Severity.ERROR))


def test_a_nonzero_threshold_alone_is_not_an_error(tmp_path: Path) -> None:
    only_distance = check_request(_request(
        tmp_path, space_threshold_m=50.0,
        time_before=dt.timedelta(0), time_after=dt.timedelta(0)))
    only_time = check_request(_request(
        tmp_path, space_threshold_m=0.0,
        time_before=dt.timedelta(minutes=1), time_after=dt.timedelta(0)))
    assert not _messages(only_distance, Severity.ERROR)
    assert not _messages(only_time, Severity.ERROR)


def test_identical_groups_in_one_case_is_a_warning_not_a_block(tmp_path: Path) -> None:
    group = EntityGroup(criteria=(Criterion(CriterionKind.IDENTIFIER, "+41790000001"),))
    problems = check_request(_request(tmp_path, group_1=group, group_2=group, case_2=None))
    warnings = _messages(problems, Severity.WARNING)
    assert any("same entities in one case" in message for message in warnings)
    assert not _messages(problems, Severity.ERROR)   # a WARNING never blocks


def test_identical_groups_across_two_cases_does_not_warn(tmp_path: Path) -> None:
    group = EntityGroup(criteria=(Criterion(CriterionKind.IDENTIFIER, "+41790000001"),))
    other_case = tmp_path / "other.matlas.sqlite"
    problems = check_request(_request(
        tmp_path, group_1=group, group_2=group, case_2=other_case))
    assert not _messages(problems, Severity.WARNING)


def test_partial_overlap_between_groups_does_not_warn(tmp_path: Path) -> None:
    problems = check_request(_request(tmp_path))   # group_1 empty, group_2 = vehicle
    assert not _messages(problems, Severity.WARNING)


def test_unwritable_output_folder_is_an_error(tmp_path: Path) -> None:
    missing_parent = tmp_path / "does" / "not" / "exist" / "out.csv"
    problems = check_request(_request(tmp_path, output_path=missing_parent))
    assert any("Cannot write results to" in message
              for message in _messages(problems, Severity.ERROR))


def test_a_fully_valid_request_has_no_problems(tmp_path: Path) -> None:
    assert check_request(_request(tmp_path)) == ()
