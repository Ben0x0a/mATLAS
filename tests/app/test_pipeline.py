"""End-to-end tests for the app-layer pipeline (model_atlas/app/pipeline.py).

Drives ``process()`` over the golden source fixture (tests/fixtures/golden: cached.csv
+ its two presets) as a FOLDER and as a ZIP, asserting the T60 cut-over contract: the
stable ``ProcessResult`` shape, the flat-CSV projection on disk, the sidecars, split
mode, and folder==zip evidence parity (equal content_uid / claim_digest sets — the
paths differ, the evidence digests must not).
Used by:    pytest.
Depends on: model_atlas.app.pipeline, the golden fixture folder.
"""
from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest

from model_atlas.app.pipeline import ProcessResult, process

_GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden"
_LINKED = "subject-1"


def _fixture_folder(tmp_path: Path) -> Path:
    """The golden source alone in a folder: cached.csv without the preset YAMLs, so the
    run's unmatched list stays empty and folder/zip runs enumerate identical files."""
    folder = tmp_path / "input"
    folder.mkdir()
    (folder / "cached.csv").write_bytes((_GOLDEN / "cached.csv").read_bytes())
    return folder


def _fixture_zip(tmp_path: Path) -> Path:
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(_GOLDEN / "cached.csv", "cached.csv")
    return archive


def _read_rows(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def _digest_pairs(rows: list[dict[str, str]]) -> set[tuple[str, str]]:
    return {(row["content_uid"], row["claim_digest"]) for row in rows}


def test_merge_run_writes_csv_and_sidecars(tmp_path: Path) -> None:
    result = process(_fixture_folder(tmp_path), _GOLDEN, tmp_path / "out",
                     linked_entity=_LINKED)
    assert isinstance(result, ProcessResult)
    assert result.output_csv == tmp_path / "out" / "merged.csv"
    assert result.output_csv.is_file()
    assert result.output_traceability is not None and result.output_traceability.is_file()
    assert result.output_warnings is not None and result.output_warnings.is_file()
    assert result.row_counts["matched"] == 1
    assert result.row_counts["rows"] == 2       # 2 source rows -> 2 assertions
    assert result.matched and not result.unmatched

    rows = _read_rows(result.output_csv)
    assert len(rows) == 2
    # The projection's mandatory provenance/derivation columns are present and filled.
    for row in rows:
        assert row["content_uid"] and row["claim_digest"]
        assert row["temporal_method"]
    # The sidecar is valid JSON and names the run's source.
    trace = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert trace["row_counts"]["rows"] == 2
    assert trace["sources"][0]["source_file"] == "cached.csv"


def test_folder_and_zip_runs_agree_on_evidence_digests(tmp_path: Path) -> None:
    folder_result = process(_fixture_folder(tmp_path), _GOLDEN, tmp_path / "out_folder",
                            linked_entity=_LINKED)
    zip_result = process(_fixture_zip(tmp_path), _GOLDEN, tmp_path / "out_zip",
                         linked_entity=_LINKED)
    folder_rows = _read_rows(folder_result.output_csv)
    zip_rows = _read_rows(zip_result.output_csv)
    assert len(folder_rows) == len(zip_rows) == 2
    # Folder==zip parity: paths differ, the evidence digests must be identical sets.
    assert _digest_pairs(folder_rows) == _digest_pairs(zip_rows)


def test_split_mode_writes_one_csv_per_preset(tmp_path: Path) -> None:
    result = process(_fixture_folder(tmp_path), _GOLDEN, tmp_path / "out",
                     linked_entity=_LINKED, merge=False)
    assert result.output_csv is None
    assert len(result.output_csvs) == 1          # one preset won the csv unit
    assert result.output_csvs[0].is_file()


def test_no_match_returns_empty_result_without_outputs(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "unrelated.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "out"
    result = process(source, _GOLDEN, out, linked_entity=_LINKED)
    assert result.output_csv is None and result.output_csvs == []
    assert result.row_counts["rows"] == 0
    assert not out.exists()                      # nothing produced -> nothing written


def test_output_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    stray = tmp_path / "out"
    stray.write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a folder"):
        process(_fixture_folder(tmp_path), _GOLDEN, stray, linked_entity=_LINKED)
