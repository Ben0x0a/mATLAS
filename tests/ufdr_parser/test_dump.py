"""End-to-end tests for run_dump: CSV output, source join, filtering, archive input."""
from __future__ import annotations

import csv
from pathlib import Path

from ufdr_parser.dump import run_dump


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_dump_writes_per_type_csvs(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_xml, out)
    base = report_xml.stem
    names = set(summary.files)
    assert names == {
        f"{base}_Location.csv",
        f"{base}_SMCoordinate.csv",
        f"{base}_Call.csv",
        f"{base}_SMParty.csv",
    }
    assert summary.record_count == 4
    assert summary.source_count == 2


def test_location_row_columns_and_source_join(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_Location.csv")
    assert len(rows) == 1
    row = rows[0]
    assert row["uuid"] == "loc1"
    assert row["Source"] == "Apple Maps"
    assert row["Name"] == ""                       # <empty/> -> blank cell
    assert row["source_path"] == "files_full.zip/private/Cache.sqlite"
    assert row["source_table"] == "ZRTCLLOCATION"
    assert row["source_offset"] == "123"


def test_submodel_uuids(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_SMCoordinate.csv")
    assert rows[0]["sub-uuid"] == "coord1"
    assert rows[0]["main-uuid"] == "loc1"
    assert rows[0]["Latitude"] == "38.34"


def test_call_source_join(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    run_dump(report_xml, out)
    rows = _read_csv(out / f"{report_xml.stem}_Call.csv")
    assert rows[0]["source_table"] == "ZCALLRECORD"
    assert rows[0]["source_offset"] == "1159850"


def test_models_filter_keeps_children_drops_others(report_xml: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_xml, out, models={"Location"})
    base = report_xml.stem
    assert set(summary.files) == {f"{base}_Location.csv", f"{base}_SMCoordinate.csv"}


def test_relations_tracked(report_xml: Path, tmp_path: Path) -> None:
    summary = run_dump(report_xml, tmp_path / "out")
    assert ("Coordinate", "Location") in summary.relations
    assert ("Party", "Call") in summary.relations


def test_dump_from_ufdr_archive(report_ufdr: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = run_dump(report_ufdr, out)
    assert summary.record_count == 4
    rows = _read_csv(out / f"{report_ufdr.stem}_Call.csv")
    assert rows[0]["uuid"] == "call1"
