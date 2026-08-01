"""Tests for gui.case_state: the case running-totals summary read from a persisted DB.

Qt-free (case_state imports only sqlite3), so no offscreen platform is needed here. Builds a
real case by running the pipeline, then asserts the totals grow across a create + append.
Used by:    pytest.
Depends on: gui.case_state, model_atlas.app.pipeline, the golden preset fixture.
"""
from __future__ import annotations

from pathlib import Path

from gui.case_state import CaseCounts, format_case_summary, read_case_counts
from model_atlas.app.pipeline import process

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
_HEADER = ("Latitude,Longitude,Accuracy (m),Speed (m/s),"
           "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy),Source,Location,Item ID\n")


def _write(path: Path, lat: str, item: str) -> Path:
    path.write_text(_HEADER + f"{lat},6.6,12.0,3.0,06.12.2025 13:00:00.000,"
                    f"/x/Cache.sqlite,Table: T(Z_PK: 1),{item}\n", encoding="utf-8")
    return path


def test_read_case_counts_grows_across_create_then_append(tmp_path: Path) -> None:
    preset = _GOLDEN / "secondary_cached.preset.yaml"
    out = tmp_path / "case"
    db = out / "op.matlas.sqlite"

    process(_write(tmp_path / "a.csv", "46.5", "1001"), preset, out,
            linked_entity="subject", base_name="op")          # create: run 1
    after_create = read_case_counts(db)
    assert after_create == CaseCounts(runs=1, sources=1, assertions=1)

    process(_write(tmp_path / "b.csv", "47.1", "1009"), preset, out,
            linked_entity="subject", base_name="op")          # append: run 2
    after_append = read_case_counts(db)
    assert after_append == CaseCounts(runs=2, sources=2, assertions=2)


def test_read_case_counts_opens_a_db_under_a_uri_significant_folder(tmp_path: Path) -> None:
    # A folder carrying a URI-significant character (here '#') must open correctly: the
    # read-only URI is built with Path.as_uri() percent-encoding, so SQLite's URI parser does
    # not mistake "Case #3" for a path + fragment. Before the fix this raised.
    preset = _GOLDEN / "secondary_cached.preset.yaml"
    out = tmp_path / "Case #3"
    db = out / "op.matlas.sqlite"
    process(_write(tmp_path / "a.csv", "46.5", "1001"), preset, out,
            linked_entity="subject", base_name="op")
    assert db.is_file()
    assert read_case_counts(db) == CaseCounts(runs=1, sources=1, assertions=1)


def test_format_case_summary() -> None:
    summary = format_case_summary("op", CaseCounts(runs=2, sources=3, assertions=512))
    assert summary == "Case 'op': 2 run(s) · 3 source(s) · 512 assertion(s)"
