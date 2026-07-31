"""Tests for the L0 writer (model_atlas/ingest/writer.py) + the container walk.

Covers:
  * a folder and a zip of the same tree -> equal source ``fingerprint_sha256`` but
    distinct ``source_id`` / ``input_file_path`` (paths-only id, §2c);
  * two identical-bytes archives at two paths -> TWO ``archive`` rows sharing ``cd_sha256``
    (a GROUP BY finding, never a merge);
  * the opt-in, preset-scoped archive skip (nothing skipped without the flag; a skip needs
    an identical preset set and still writes ``run_archive(skipped=1)``; a changed preset
    digest under the same key+version forbids the skip);
  * nested ``a.zip!b.zip!x.db`` vs ``c.zip!b.zip!x.db`` do not collide;
  * a persisted ``*.matlas.sqlite`` fed as input is refused (the self-ingestion guard).
"""
from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

from model_atlas.core.ids import preset_id
from model_atlas.core.store import RunStore
from model_atlas.core.vocabularies import Preset
from model_atlas.ingest.writer import SelfIngestionError, ingest_input, is_matlas_run_db


# --- fixtures ---------------------------------------------------------------

def _preset(key: str = "p1", version: str = "1", digest: str = "d1") -> Preset:
    return Preset(preset_id=preset_id(key, version), key=key, name=key,
                  version=version, definition_digest=digest)


def _csv_bytes() -> bytes:
    return b"id,lat\n1,46.193000\n2,47.000000\n"


def _zip_of(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _sqlite_bytes(table: str = "t") -> bytes:
    """A tiny SQLite db as raw bytes (written to a temp file, then read back)."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=True) as tmp:
        conn = sqlite3.connect(tmp.name)
        conn.execute(f"CREATE TABLE {table} (id INTEGER, v TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES (1, 'a')")
        conn.commit()
        conn.close()
        return Path(tmp.name).read_bytes()


def _sources(store: RunStore) -> list[dict]:
    cur = store.connection.execute(
        "SELECT source_id, input_file_path, inner_path, subunit, fingerprint_sha256 FROM source"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --- folder == zip (D29), paths-only id (§2c) -------------------------------

def test_folder_and_zip_share_fingerprint_but_not_source_id(tmp_path: Path) -> None:
    folder = tmp_path / "tree"
    folder.mkdir()
    (folder / "x.csv").write_bytes(_csv_bytes())
    zip_path = tmp_path / "tree.zip"
    zip_path.write_bytes(_zip_of({"x.csv": _csv_bytes()}))

    with RunStore() as store:
        ingest_input(folder, store, case_number="C1")
        ingest_input(zip_path, store, case_number="C1")
        rows = _sources(store)

    csv_rows = [r for r in rows if r["subunit"] is None]
    assert len(csv_rows) == 2
    assert csv_rows[0]["fingerprint_sha256"] == csv_rows[1]["fingerprint_sha256"]
    assert csv_rows[0]["source_id"] != csv_rows[1]["source_id"]
    assert {r["input_file_path"] for r in csv_rows} == {
        str((folder / "x.csv").resolve()), str(zip_path.resolve())
    }
    # the zip source carries a chain-inclusive inner_path; the folder file does not
    assert sorted(r["inner_path"] or "" for r in csv_rows) == ["", "tree.zip!x.csv"]


def test_append_adds_a_run_to_the_existing_case(tmp_path: Path) -> None:
    # T94: existing_case_id switches ingest_input to APPEND — a new run under the SAME case,
    # never a second case row.
    f1 = tmp_path / "t1"; f1.mkdir(); (f1 / "a.csv").write_bytes(_csv_bytes())
    f2 = tmp_path / "t2"; f2.mkdir(); (f2 / "b.csv").write_bytes(b"id,v\n7,z\n")
    with RunStore() as store:
        first = ingest_input(f1, store, case_number="Operation Falcon")
        second = ingest_input(f2, store, case_number="ignored-on-append",
                              existing_case_id=first.case_id)
        assert first.case_id == second.case_id      # one case
        assert first.run_id != second.run_id        # two runs
        counts = {
            t: store.connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("case_metadata", "run", "source")
        }
    assert counts == {"case_metadata": 1, "run": 2, "source": 2}


def test_records_written_verbatim(tmp_path: Path) -> None:
    (tmp_path / "x.csv").write_bytes(_csv_bytes())
    with RunStore() as store:
        report = ingest_input(tmp_path / "x.csv", store, case_number="C1")
        assert report.records == 2
        raw = store.connection.execute(
            "SELECT raw_json FROM raw_record ORDER BY line_no"
        ).fetchall()
    # canonical JSON rule 5: the trailing zeros of "46.193000" survive verbatim
    assert '"lat":"46.193000"' in raw[0][0]


# --- two identical-bytes archives -> two rows sharing cd_sha256 (D38) --------

def test_two_identical_archives_two_rows_one_cd_sha256(tmp_path: Path) -> None:
    payload = _zip_of({"x.csv": _csv_bytes()})
    folder = tmp_path / "acq"
    folder.mkdir()
    (folder / "a.zip").write_bytes(payload)
    (folder / "b.zip").write_bytes(payload)

    with RunStore() as store:
        report = ingest_input(folder, store, case_number="C1")
        archives = store.connection.execute(
            "SELECT path, cd_sha256 FROM archive ORDER BY path"
        ).fetchall()
        dupes = store.connection.execute(
            "SELECT copies FROM duplicate_sources"
        ).fetchall()

    assert report.archives == 2
    assert len(archives) == 2
    assert archives[0][0] != archives[1][0]              # two distinct paths
    assert archives[0][1] == archives[1][1]              # one shared cd_sha256
    # the two byte-identical csv sources also surface in the duplicate-sources finding view
    assert dupes and dupes[0][0] == 2


# --- opt-in, preset-scoped skip (D38/D40) -----------------------------------

def _cd_sha256_of(zip_path: Path) -> str:
    from model_atlas.ingest.discover import _cd_sha256_from_path

    return _cd_sha256_from_path(zip_path)


def test_no_skip_without_the_opt_in_flag(tmp_path: Path) -> None:
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(_zip_of({"x.csv": _csv_bytes()}))
    presets = (_preset(),)
    from model_atlas.ingest.writer import _preset_signature

    prior = {_cd_sha256_of(zip_path): _preset_signature(presets)}
    with RunStore() as store:
        # prior map matches, but the opt-in flag is OFF -> nothing is skipped
        report = ingest_input(
            zip_path, store, case_number="C1", presets=presets,
            skip_reparsed_archives=False, prior_archive_presets=prior,
        )
    assert report.archives == 1
    assert report.archives_skipped == 0
    assert report.sources == 1


def test_skip_with_identical_preset_set_still_links(tmp_path: Path) -> None:
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(_zip_of({"x.csv": _csv_bytes()}))
    presets = (_preset(),)
    from model_atlas.ingest.writer import _preset_signature

    prior = {_cd_sha256_of(zip_path): _preset_signature(presets)}
    with RunStore() as store:
        report = ingest_input(
            zip_path, store, case_number="C1", presets=presets,
            skip_reparsed_archives=True, prior_archive_presets=prior,
        )
        link = store.connection.execute(
            "SELECT skipped FROM run_archive"
        ).fetchone()
    assert report.archives == 1
    assert report.archives_skipped == 1
    assert report.sources == 0          # the subtree was not walked
    assert link[0] == 1                 # ...but the encounter is recorded, never traceless


def test_changed_preset_digest_forbids_the_skip(tmp_path: Path) -> None:
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(_zip_of({"x.csv": _csv_bytes()}))
    from model_atlas.ingest.writer import _preset_signature

    # prior run parsed with digest d1; this run selects the SAME key+version but digest d2
    prior = {_cd_sha256_of(zip_path): _preset_signature((_preset(digest="d1"),))}
    with RunStore() as store:
        report = ingest_input(
            zip_path, store, case_number="C1", presets=(_preset(digest="d2"),),
            skip_reparsed_archives=True, prior_archive_presets=prior,
        )
    assert report.archives_skipped == 0     # a different definition_digest forbids the skip
    assert report.sources == 1


# --- nested containers do not collide (R10) ---------------------------------

def test_nested_archives_do_not_collide(tmp_path: Path) -> None:
    inner = _zip_of({"x.db": _sqlite_bytes()})
    (tmp_path / "a.zip").write_bytes(_zip_of({"b.zip": inner}))
    (tmp_path / "c.zip").write_bytes(_zip_of({"b.zip": inner}))
    folder = tmp_path

    with RunStore() as store:
        ingest_input(folder, store, case_number="C1", max_container_depth=2)
        rows = _sources(store)

    db_rows = [r for r in rows if (r["inner_path"] or "").endswith("x.db")]
    assert len(db_rows) == 2
    inners = sorted(r["inner_path"] for r in db_rows)
    assert inners == ["a.zip!b.zip!x.db", "c.zip!b.zip!x.db"]
    assert db_rows[0]["source_id"] != db_rows[1]["source_id"]
    # byte-identical inner db -> the two sources share a fingerprint (a finding, not a merge)
    assert db_rows[0]["fingerprint_sha256"] == db_rows[1]["fingerprint_sha256"]


# --- self-ingestion guard (R16) ---------------------------------------------

def test_persisted_run_db_is_refused(tmp_path: Path) -> None:
    persisted = tmp_path / "case.matlas.sqlite"
    with RunStore() as store:
        store.persist(persisted)
    assert is_matlas_run_db(persisted) is True

    with RunStore() as store:
        try:
            ingest_input(persisted, store, case_number="C1")
        except SelfIngestionError:
            pass
        else:
            raise AssertionError("expected SelfIngestionError for a persisted run DB")


# --- FIX-11 #1: archive re-append idempotency -------------------------------

def test_reappending_the_same_archive_to_a_case_is_idempotent(tmp_path: Path) -> None:
    # The archive row is path-keyed (UNIQUE(path)); re-appending the SAME zip to an existing
    # case must NOT collide and abort the append. The existing archive row is skipped, this
    # run's run_archive link is still recorded, and the already-present source is skipped.
    zip_path = tmp_path / "a.zip"
    zip_path.write_bytes(_zip_of({"x.csv": _csv_bytes()}))
    with RunStore() as store:
        first = ingest_input(zip_path, store, case_number="C1")
        second = ingest_input(zip_path, store, case_number="ignored-on-append",
                              existing_case_id=first.case_id)     # must not raise
        archives = store.connection.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        run_archives = store.connection.execute("SELECT COUNT(*) FROM run_archive").fetchone()[0]
        sources = store.connection.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    assert first.case_id == second.case_id
    assert archives == 1                         # one path -> one archive row across both runs
    assert run_archives == 2                     # each run records its OWN encounter link
    assert second.sources_already_in_case == 1   # the source is recognised + skipped
    assert sources == 1                          # not re-ingested


# --- FIX-08 #3: orphan sidecar handling -------------------------------------

def test_orphan_sidecar_emitted_but_live_sidecar_skipped(tmp_path: Path) -> None:
    folder = tmp_path / "acq"
    folder.mkdir()
    (folder / "main.csv").write_bytes(_csv_bytes())            # a real source...
    (folder / "main.csv-wal").write_bytes(b"junk wal")         # ...its live -wal sibling: skipped
    (folder / "lonely.sqlite-journal").write_bytes(b"orphan")  # an ORPHAN sidecar: must be emitted
    with RunStore() as store:
        report = ingest_input(folder, store, case_number="C1")
        names = {Path(r["input_file_path"]).name for r in _sources(store)}
    # only the csv is a source — the live -wal beside its sibling was skipped as a sidecar...
    assert names == {"main.csv"}
    assert report.sources == 1
    # ...but the orphan -journal (no sibling db) reached the writer (no reader -> surfaced
    # unreadable), never silently dropped during the walk.
    assert report.skipped_unreadable == 1


def test_plain_sqlite_is_not_flagged_as_a_run_db(tmp_path: Path) -> None:
    db = tmp_path / "plain.sqlite"
    db.write_bytes(_sqlite_bytes())
    assert is_matlas_run_db(db) is False
    with RunStore() as store:
        report = ingest_input(db, store, case_number="C1")
    assert report.sources == 1
    assert report.flagged_self_ingest == 0
