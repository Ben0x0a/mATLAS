"""End-to-end tests for the app-layer pipeline (model_atlas/app/pipeline.py).

Drives ``process()`` over the golden source fixture (tests/fixtures/golden: cached.csv
+ its two presets) as a FOLDER and as a ZIP, asserting the relational-engine contract: the
stable ``ProcessResult`` shape, the mandatory case database, the flat-CSV projection on
disk (one file per format), the sidecars, create-then-append determinism, and folder==zip
evidence parity (equal observation_content_uid / spatiotemporal_entity_hash sets — the paths differ, the evidence
digests must not).
Used by:    pytest.
Depends on: model_atlas.app.pipeline, the golden fixture folder.
"""
from __future__ import annotations

import csv
import json
import sqlite3
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
    folder.mkdir(parents=True)
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
    return {(row["observation_content_uid_matlas"], row["spatiotemporal_entity_hash_matlas"]) for row in rows}


def test_source_tier_follows_the_matched_preset(tmp_path: Path) -> None:
    # #1: the matched preset's declared tier ('secondary') overrides the L0 'primary'
    # default — previously the preset's tier was ignored and everything stamped primary.
    secondary = _GOLDEN / "secondary_cached.preset.yaml"
    result = process(_fixture_folder(tmp_path), secondary, tmp_path / "out",
                     concerns=_LINKED)
    rows = _read_rows(result.output_csv)
    assert rows and all(row["source_tier_matlas"] == "secondary" for row in rows)


def test_run_writes_case_db_csv_and_sidecars(tmp_path: Path) -> None:
    result = process(_fixture_folder(tmp_path), _GOLDEN, tmp_path / "out",
                     concerns=_LINKED)
    assert isinstance(result, ProcessResult)
    # The case database is the mandatory default output, always written.
    assert result.output_case_db == tmp_path / "out" / "merged.matlas.sqlite"
    assert result.output_case_db.is_file()
    assert result.output_csv == tmp_path / "out" / "merged.csv"
    assert result.output_csv.is_file()
    assert result.output_traceability is not None and result.output_traceability.is_file()
    assert result.output_warnings is not None and result.output_warnings.is_file()
    assert result.row_counts["matched"] == 1
    assert result.row_counts["rows"] == 2       # 2 source rows -> 2 observations
    assert result.matched and not result.unmatched

    rows = _read_rows(result.output_csv)
    assert len(rows) == 2
    # The projection's mandatory provenance/derivation columns are present and filled.
    for row in rows:
        assert row["observation_content_uid_matlas"] and row["spatiotemporal_entity_hash_matlas"]
        assert row["time_lower_unix_utc_us"]
    # The sidecar is valid JSON and names the run's source.
    trace = json.loads(result.output_traceability.read_text(encoding="utf-8"))
    assert trace["row_counts"]["rows"] == 2
    assert trace["sources"][0]["source_file"] == "cached.csv"


def test_refuses_to_overwrite_a_non_matlas_file_at_the_output_path(tmp_path: Path) -> None:
    # A pre-existing NON-mATLAS file at exactly <output>/<name>.matlas.sqlite must NOT be
    # silently destroyed: on CREATE, store.persist() unlinks the target before VACUUM INTO, so
    # a run there would delete an unrelated/corrupt file. The pipeline refuses instead.
    out = tmp_path / "out"
    out.mkdir()
    stray = out / "merged.matlas.sqlite"
    payload = b"not a sqlite database at all"
    stray.write_bytes(payload)

    with pytest.raises(FileExistsError, match="not a mATLAS case database"):
        process(_fixture_folder(tmp_path), _GOLDEN, out, concerns=_LINKED)

    # The stray file is untouched (byte-for-byte) — nothing was deleted or overwritten.
    assert stray.read_bytes() == payload


def test_folder_and_zip_runs_agree_on_evidence_digests(tmp_path: Path) -> None:
    folder_result = process(_fixture_folder(tmp_path), _GOLDEN, tmp_path / "out_folder",
                            concerns=_LINKED)
    zip_result = process(_fixture_zip(tmp_path), _GOLDEN, tmp_path / "out_zip",
                         concerns=_LINKED)
    folder_rows = _read_rows(folder_result.output_csv)
    zip_rows = _read_rows(zip_result.output_csv)
    assert len(folder_rows) == len(zip_rows) == 2
    # Folder==zip parity: paths differ, the evidence digests must be identical sets.
    assert _digest_pairs(folder_rows) == _digest_pairs(zip_rows)


def test_append_builds_the_same_case_as_the_union_of_runs(tmp_path: Path) -> None:
    # Append determinism: creating a case from evidence A then APPENDING evidence B yields
    # exactly the digest set of A-alone plus B-alone — the same rows, same identity, whichever
    # run adds them (no run/case id feeds a digest).
    import sqlite3

    preset = _GOLDEN / "secondary_cached.preset.yaml"
    header = ("Latitude,Longitude,Accuracy (m),Speed (m/s),"
              "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy),Source,Location,Item ID\n")
    file_a = tmp_path / "a.csv"
    file_a.write_text(header + "46.5197,6.6323,12.0,3.0,06.12.2025 13:00:00.000,"
                      "/x/Cache.sqlite,Table: T(Z_PK: 1),1001\n", encoding="utf-8")
    file_b = tmp_path / "b.csv"
    file_b.write_text(header + "47.1000,7.2000,5.0,1.0,07.12.2025 09:00:00.000,"
                      "/x/Cache.sqlite,Table: T(Z_PK: 9),1009\n", encoding="utf-8")

    def digests(db: Path) -> set:
        con = sqlite3.connect(db)
        try:
            return set(con.execute("SELECT observation_content_hash, spatiotemporal_entity_hash FROM observation").fetchall())
        finally:
            con.close()

    process(file_a, preset, tmp_path / "oa", concerns=_LINKED)          # A alone
    process(file_b, preset, tmp_path / "ob", concerns=_LINKED)          # B alone
    d_a = digests(tmp_path / "oa" / "merged.matlas.sqlite")
    d_b = digests(tmp_path / "ob" / "merged.matlas.sqlite")

    out = tmp_path / "case"
    process(file_a, preset, out, concerns=_LINKED)                      # create case
    process(file_b, preset, out, concerns=_LINKED)                      # APPEND to it
    d_full = digests(out / "merged.matlas.sqlite")

    assert d_a and d_b and d_a != d_b
    assert d_full == d_a | d_b       # append adds exactly B's rows; A's identity is unchanged
    # and it is genuinely one case with two runs
    con = sqlite3.connect(out / "merged.matlas.sqlite")
    try:
        assert con.execute("SELECT COUNT(*) FROM case_metadata").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 2
    finally:
        con.close()


def test_re_appending_the_same_evidence_is_a_recorded_no_op(tmp_path: Path) -> None:
    # T98: re-appending evidence ALREADY in the case must not crash. The duplicate source is
    # skipped and recorded (no silent loss), the case is unchanged, and the second run is still
    # recorded (a documented no-op run, like a no-match run).
    import sqlite3

    preset = _GOLDEN / "secondary_cached.preset.yaml"
    header = ("Latitude,Longitude,Accuracy (m),Speed (m/s),"
              "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy),Source,Location,Item ID\n")
    evidence = tmp_path / "a.csv"
    evidence.write_text(header + "46.5197,6.6323,12.0,3.0,06.12.2025 13:00:00.000,"
                        "/x/Cache.sqlite,Table: T(Z_PK: 1),1001\n", encoding="utf-8")

    out = tmp_path / "case"
    process(evidence, preset, out, concerns=_LINKED)                      # create
    result = process(evidence, preset, out, concerns=_LINKED)             # re-append SAME

    # The skip is recorded, not silent, and the run added nothing new.
    assert any("already in the case" in warning for warning in result.warnings)
    assert result.row_counts["matched"] == 0
    con = sqlite3.connect(out / "merged.matlas.sqlite")
    try:
        assert con.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 2       # both runs recorded
        assert con.execute("SELECT COUNT(*) FROM source_file").fetchone()[0] == 1    # no duplicate source
        assert con.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 1 # unchanged (1 row)
    finally:
        con.close()


def test_no_match_writes_the_case_db_but_no_csv(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    (source / "unrelated.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "out"
    result = process(source, _GOLDEN, out, concerns=_LINKED)
    assert result.output_csv is None
    assert result.output_case_db == out / "merged.matlas.sqlite"
    assert result.row_counts["rows"] == 0
    # The case database always records the run (a documented no-match result); the CSV and
    # other projections are written only when there are observations.
    assert (out / "merged.matlas.sqlite").is_file()
    assert not (out / "merged.csv").exists()


def test_two_independent_runs_produce_identical_digests(tmp_path: Path) -> None:
    # Determinism spot-check: the SAME evidence processed twice into SEPARATE cases yields
    # byte-identical observation_content_hash/spatiotemporal_entity_hash sets and an identical CSV projection — nothing
    # run-, time-, or order-dependent leaks into an id, a digest, or the output.
    import sqlite3

    def digests(db: Path) -> set:
        con = sqlite3.connect(db)
        try:
            return set(con.execute("SELECT observation_content_hash, spatiotemporal_entity_hash FROM observation").fetchall())
        finally:
            con.close()

    source = _fixture_folder(tmp_path)
    first = process(source, _GOLDEN, tmp_path / "o1", concerns=_LINKED)
    second = process(source, _GOLDEN, tmp_path / "o2", concerns=_LINKED)

    d1 = digests(first.output_case_db)
    assert d1 and d1 == digests(second.output_case_db)             # identical evidence identity

    # The CSVs must match on every column EXCEPT the id columns, which are random per run
    # since decision 50. That is the honest form of this assertion: it tests the invariant
    # that survives (nothing run-, time- or order-dependent reaches a digest or a projected
    # VALUE) instead of a reproducibility the ids deliberately no longer have.
    import csv as _csv
    def rows_without_ids(path):
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = _csv.reader(handle, delimiter=";")
            header = next(reader)
            keep = [i for i, name in enumerate(header)
                    if not name.endswith(("_uid", "_uid_matlas", "row_uid_matlas"))]
            return [header] + [[row[i] for i in keep] for row in reader]
    assert rows_without_ids(first.output_csv) == rows_without_ids(second.output_csv)


def _mixed_input(tmp_path: Path) -> Path:
    """The golden CSV beside evidence NO preset targets: an unrelated CSV and a SQLite db
    with two tables — the shape of a real acquisition, where one preset targets one file."""
    folder = tmp_path / "input"
    folder.mkdir(parents=True)
    (folder / "cached.csv").write_bytes((_GOLDEN / "cached.csv").read_bytes())
    (folder / "unrelated.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    con = sqlite3.connect(folder / "keychain.sqlite")
    con.execute("CREATE TABLE analytics (id INTEGER)")
    con.execute("CREATE TABLE secrets (id INTEGER)")
    con.commit()
    con.close()
    return folder


def _ingested_units(case_db: Path) -> set:
    con = sqlite3.connect(case_db)
    try:
        return set(con.execute("SELECT input_file_path, subunit FROM source_file").fetchall())
    finally:
        con.close()


def test_l0_opens_only_what_the_presets_target(tmp_path: Path) -> None:
    # The default run must not read the whole acquisition to apply one preset: only the file
    # a selector locates becomes a source row. Previously every db and every table was read
    # first and matched afterwards — minutes of work for one file's worth of evidence.
    result = process(_mixed_input(tmp_path), _GOLDEN, tmp_path / "out", concerns=_LINKED)
    units = _ingested_units(result.output_case_db)
    assert {Path(path).name for path, _subunit in units} == {"cached.csv"}
    assert result.row_counts["rows"] == 2       # the evidence itself is unaffected


def test_untargeted_neighbours_do_not_change_evidence_identity(tmp_path: Path) -> None:
    # Targeting decides what a run OPENS, never what a source IS once opened: the same
    # cached.csv yields the same observation_content_uid/spatiotemporal_entity_hash set whether it sits alone or beside
    # evidence the presets pass over.
    crowded = process(_mixed_input(tmp_path / "crowded"), _GOLDEN, tmp_path / "out1",
                      concerns=_LINKED)
    alone = process(_fixture_folder(tmp_path / "alone"), _GOLDEN, tmp_path / "out2",
                    concerns=_LINKED)
    assert _digest_pairs(_read_rows(crowded.output_csv)) == \
        _digest_pairs(_read_rows(alone.output_csv))


def test_output_path_that_is_a_file_is_refused(tmp_path: Path) -> None:
    stray = tmp_path / "out"
    stray.write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a folder"):
        process(_fixture_folder(tmp_path), _GOLDEN, stray, concerns=_LINKED)
