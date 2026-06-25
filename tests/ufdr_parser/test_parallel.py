"""Tests for parallel CSV flushing: equivalence with the serial path + worker sizing."""
from __future__ import annotations

import csv
from pathlib import Path

from ufdr_parser.dump import run_dump
from ufdr_parser.parallel import choose_workers


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_parallel_flush_matches_serial(report_xml: Path, tmp_path: Path) -> None:
    serial = run_dump(report_xml, tmp_path / "serial", workers=1)
    parallel = run_dump(report_xml, tmp_path / "parallel", workers=2)

    assert serial.files == parallel.files
    base = report_xml.stem
    for name in serial.files:
        s_rows = _read(tmp_path / "serial" / name)
        p_rows = _read(tmp_path / "parallel" / name)
        assert s_rows == p_rows, f"{name} differs between serial and parallel flush"
    # Sanity: the source join still happened in the parallel path.
    loc = _read(tmp_path / "parallel" / f"{base}_Location.csv")
    assert loc[0]["source_table"] == "ZRTCLLOCATION"


def test_choose_workers_at_least_one() -> None:
    assert choose_workers() >= 1
    assert choose_workers(cap=1) == 1


def test_parallel_parse_matches_serial_xml(report_xml: Path, tmp_path: Path) -> None:
    serial = run_dump(report_xml, tmp_path / "serial", parse_workers=1)
    parallel = run_dump(report_xml, tmp_path / "parallel", parse_workers=2)

    assert set(serial.files) == set(parallel.files)
    assert serial.record_count == parallel.record_count
    assert serial.relations == parallel.relations
    for name in serial.files:
        s_rows = _read(tmp_path / "serial" / name)
        p_rows = _read(tmp_path / "parallel" / name)
        assert s_rows == p_rows, f"{name} differs between serial and by-type parallel parse"


def test_parallel_parse_matches_serial_ufdr(report_ufdr: Path, tmp_path: Path) -> None:
    serial = run_dump(report_ufdr, tmp_path / "serial", parse_workers=1)
    parallel = run_dump(report_ufdr, tmp_path / "parallel", parse_workers=2)
    assert set(serial.files) == set(parallel.files)
    base = report_ufdr.stem
    call = _read(tmp_path / "parallel" / f"{base}_Call.csv")
    assert call[0]["source_table"] == "ZCALLRECORD"  # source join intact in parallel path
