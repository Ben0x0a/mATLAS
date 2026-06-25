"""Tests for the model_signatures drift check: establish, no-shift, and shift cases."""
from __future__ import annotations

from pathlib import Path

from ufdr_parser.dump import DumpSummary
from ufdr_parser.model_signatures import Baseline, check_drift


def _summary(
    types_by_level: dict[int, set[str]],
    relations: set[tuple[str, str]],
    fields_by_type: dict[tuple[int, str], set[str]],
) -> DumpSummary:
    return DumpSummary(
        types_by_level=types_by_level,
        relations=relations,
        fields_by_type=fields_by_type,
    )


def _baseline_summary() -> DumpSummary:
    return _summary(
        types_by_level={0: {"Location"}, 1: {"Coordinate"}},
        relations={("Coordinate", "Location")},
        fields_by_type={
            (0, "Location"): {"Source", "TimeStamp"},
            (1, "Coordinate"): {"Latitude", "Longitude"},
        },
    )


def test_first_run_establishes_baseline(tmp_path: Path) -> None:
    db = tmp_path / "baseline.db"
    report = check_drift(_baseline_summary(), db)
    assert report.baseline_established is True
    assert report.has_shifts is False
    # The baseline is now populated.
    base = Baseline(db)
    try:
        assert (0, "Location") in base.known_model_types()
        assert (0, "Location", "Source") in base.known_fields()
    finally:
        base.close()


def test_second_identical_run_has_no_shifts(tmp_path: Path) -> None:
    db = tmp_path / "baseline.db"
    check_drift(_baseline_summary(), db)
    report = check_drift(_baseline_summary(), db)
    assert report.baseline_established is False
    assert report.has_shifts is False


def test_new_type_field_and_relation_are_flagged(tmp_path: Path) -> None:
    db = tmp_path / "baseline.db"
    check_drift(_baseline_summary(), db)

    drifted = _summary(
        types_by_level={0: {"Location", "Call"}, 1: {"Coordinate"}},
        relations={("Coordinate", "Location"), ("Party", "Call")},
        fields_by_type={
            (0, "Location"): {"Source", "TimeStamp", "Accuracy"},  # +Accuracy
            (0, "Call"): {"Direction"},
            (1, "Coordinate"): {"Latitude", "Longitude"},
        },
    )
    report = check_drift(drifted, db)
    assert (0, "Call") in report.new_model_types
    assert ("Party", "Call") in report.new_relations
    assert (0, "Location", "Accuracy") in report.new_fields
    assert report.has_shifts is True


def test_removed_field_flagged_only_when_type_present(tmp_path: Path) -> None:
    db = tmp_path / "baseline.db"
    check_drift(_baseline_summary(), db)

    # Location is present but lost its TimeStamp field; Coordinate is absent this run.
    drifted = _summary(
        types_by_level={0: {"Location"}},
        relations=set(),
        fields_by_type={(0, "Location"): {"Source"}},
    )
    report = check_drift(drifted, db)
    assert (0, "Location", "TimeStamp") in report.removed_fields
    # Coordinate's fields are NOT reported removed: the whole type just did not appear.
    assert all(mt != "Coordinate" for _level, mt, _name in report.removed_fields)
