"""Tests for model_atlas/analyse/concomitance/output.py (the Verify list).

Covers:     ``CsvMatchSink`` writes the exact header, streams row-by-row without holding
            the result set, ``close()`` is idempotent, and the same input twice produces
            a byte-identical file (determinism).
Uses:       ``model_atlas.analyse.concomitance`` only — Qt-free, no sqlite3.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.analyse.concomitance import CSV_HEADER, CsvMatchSink, MatchRow


def _row(observation_a: str = "a1", observation_b: str = "a2") -> MatchRow:
    return MatchRow(
        g1_identifier="+41790000001", g1_identifier_type="msisdn", g1_entity_type="device",
        g1_latitude=46.2030, g1_longitude=6.1430, g1_accuracy_m=5.0,
        g1_time_start="2026-03-14T02:00:00+00:00", g1_time_end="2026-03-14T02:05:00+00:00",
        g1_observation_id=observation_a, g1_source="case.csv",
        g2_identifier="GE123456", g2_identifier_type="plate", g2_entity_type="vehicle",
        g2_latitude=46.2031, g2_longitude=6.1431, g2_accuracy_m=None,
        g2_time_start="2026-03-14T02:02:00+00:00", g2_time_end="2026-03-14T02:06:00+00:00",
        g2_observation_id=observation_b, g2_source=None,
        distance_m=12.34, distance_within_accuracy=False,
        time_delta_s=-30.0, time_overlap=False)


def test_header_matches_the_task_contract_exactly() -> None:
    assert CSV_HEADER == (
        "g1_identifier", "g1_identifier_type", "g1_entity_type",
        "g1_latitude", "g1_longitude", "g1_accuracy_m",
        "g1_time_start", "g1_time_end",
        "g1_observation_id", "g1_source",
        "g2_identifier", "g2_identifier_type", "g2_entity_type",
        "g2_latitude", "g2_longitude", "g2_accuracy_m",
        "g2_time_start", "g2_time_end",
        "g2_observation_id", "g2_source",
        "distance_m", "distance_within_accuracy", "time_delta_s", "time_overlap",
    )


def test_writes_header_and_streams_rows(tmp_path: Path) -> None:
    path = tmp_path / "matches.csv"
    sink = CsvMatchSink(path)
    sink.open()
    sink.write(_row("a1", "a2"))
    sink.write(_row("a3", "a4"))
    sink.close()

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")   # utf-8-sig BOM (house rule)
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert len(lines) == 3   # header + 2 rows
    assert lines[1].startswith("+41790000001,msisdn,device")
    assert "a1" in lines[1] and "a2" in lines[1]
    assert "a3" in lines[2] and "a4" in lines[2]


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "matches.csv"
    sink = CsvMatchSink(path)
    sink.open()
    sink.write(_row())
    sink.close()
    sink.close()   # must not raise
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_same_input_twice_is_byte_identical(tmp_path: Path) -> None:
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    for path in (first, second):
        sink = CsvMatchSink(path)
        sink.open()
        sink.write(_row("a1", "a2"))
        sink.write(_row("a3", "a4"))
        sink.close()
    assert first.read_bytes() == second.read_bytes()
