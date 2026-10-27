"""Tests for the normative determinism rules (``model_atlas.core.determinism``).

Defines:    one test per doc rule (run_db_schema.md §Determinism, rules 1-7) plus the
            three mandated end-to-end checks: folder==zip digest parity THROUGH the
            store, the double-run diff (byte-identical ``iterdump`` output modulo the
            run-carrying columns), and the canonicalised ``|``-in-path forgery case.
Used by:    the verification gate (pytest tests/core/test_determinism.py).
Depends on: ``model_atlas.core.determinism`` (the module under test), ``ids`` /
            ``vocabularies`` / ``store`` (the end-to-end surfaces), and pytest.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from model_atlas.core import determinism
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore


# --- rule 1: SQLite enumeration order ---------------------------------------

def test_sqlite_enumeration_is_rowid_order() -> None:
    cx = sqlite3.connect(":memory:")
    try:
        cx.execute("CREATE TABLE t (name TEXT)")
        cx.executemany("INSERT INTO t VALUES (?)",
                       [("charlie",), ("alpha",), ("bravo",)])
        rows = [r[0] for r in cx.execute(determinism.sqlite_enumeration_sql(cx, "t"))]
        # Insertion (rowid) order — never re-sorted by value.
        assert rows == ["charlie", "alpha", "bravo"]
    finally:
        cx.close()


def test_sqlite_without_rowid_enumeration_is_primary_key_order() -> None:
    cx = sqlite3.connect(":memory:")
    try:
        cx.execute("CREATE TABLE w (k TEXT, v TEXT, PRIMARY KEY (k)) WITHOUT ROWID")
        cx.executemany("INSERT INTO w VALUES (?, ?)", [("b", "2"), ("a", "1")])
        rows = [r[0] for r in cx.execute(determinism.sqlite_enumeration_sql(cx, "w"))]
        assert rows == ["a", "b"]   # PK order, since there is no rowid to pin on
    finally:
        cx.close()


def test_without_rowid_with_noncanonical_spacing_enumerates_in_pk_order() -> None:
    # A WITHOUT ROWID table whose clause carries non-canonical spacing ("WITHOUT  ROWID")
    # must still be detected (a schema-substring test would false-negative and try to
    # ORDER BY rowid on a rowid-less table, which raises).
    cx = sqlite3.connect(":memory:")
    try:
        cx.execute("CREATE TABLE w (k TEXT, v TEXT, PRIMARY KEY (k)) WITHOUT  ROWID")
        cx.executemany("INSERT INTO w VALUES (?, ?)", [("b", "2"), ("a", "1")])
        rows = [r[0] for r in cx.execute(determinism.sqlite_enumeration_sql(cx, "w"))]
        assert rows == ["a", "b"]   # PK order — no error despite the double space
    finally:
        cx.close()


def test_normal_table_with_without_rowid_in_a_comment_enumerates_in_rowid_order() -> None:
    # The phrase "WITHOUT ROWID" inside a comment must NOT be mistaken for the real clause;
    # this is an ordinary rowid table and must enumerate in insertion (rowid) order.
    cx = sqlite3.connect(":memory:")
    try:
        cx.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT /* not WITHOUT ROWID */)")
        cx.executemany("INSERT INTO t VALUES (?, ?)",
                       [("charlie", "3"), ("alpha", "1"), ("bravo", "2")])
        rows = [r[0] for r in cx.execute(determinism.sqlite_enumeration_sql(cx, "t"))]
        assert rows == ["charlie", "alpha", "bravo"]   # rowid order, NOT re-sorted by PK
    finally:
        cx.close()


def test_sqlite_enumeration_of_an_unknown_table_raises() -> None:
    cx = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError):
            determinism.sqlite_enumeration_sql(cx, "missing")
    finally:
        cx.close()


def test_recovered_rows_number_after_live_rows_continuing_the_sequence() -> None:
    numbered = list(determinism.numbered_records(["live-1", "live-2"],
                                                 ["wal-1", "freelist-1"]))
    # Recovered rows come AFTER all live rows and CONTINUE the same line_no sequence;
    # the flag is what the reader stores as the recovery_state marker.
    assert numbered == [(1, "live-1", False), (2, "live-2", False),
                        (3, "wal-1", True), (4, "freelist-1", True)]


# --- rule 2: CSV line numbering ----------------------------------------------

def test_csv_line_no_excludes_header_and_counts_blanks() -> None:
    physical = ["a;b", "1;2", "", "3;4"]
    # The header is excluded from numbering; the blank line is COUNTED (and yielded).
    assert list(determinism.csv_data_line_numbers(physical)) == [
        (1, "1;2"), (2, ""), (3, "3;4")]


# --- rule 3: Excel line numbering ---------------------------------------------

def test_excel_line_no_is_the_sheet_row_itself() -> None:
    # Identity: a blank row 4 shifts nothing — row 5 stays line_no 5.
    assert determinism.excel_line_no(5) == 5
    with pytest.raises(ValueError):
        determinism.excel_line_no(0)


# --- rule 4: path canonicalisation ---------------------------------------------

def test_canonical_path_nfc_unifies_platform_encodings() -> None:
    # Explicit escapes, NOT literal accents: an editor re-normalising this file
    # must never be able to silently unify the two forms and vacuate the test.
    composed = "/case/caf\u00e9/f.csv"      # é as one code point (NFC)
    decomposed = "/case/cafe\u0301/f.csv"   # e + combining acute (NFD, macOS)
    assert composed != decomposed            # sanity: the inputs really differ
    assert determinism.canonical_path(composed) == determinism.canonical_path(decomposed)
    # …and therefore ONE source row, not two, for the same exhibit: the source's identity
    # is now its NATURAL KEY (UNIQUE on input_file_path/inner_path/subunit), and the
    # canonical path is what goes into it. Ids themselves are random (decision 50), so the
    # property is asserted where it now lives — on the key, not on a derived uuid.


def test_canonical_path_normalises_separators_and_requires_absolute() -> None:
    assert determinism.canonical_path("C:\\case\\f.csv") == "C:/case/f.csv"
    with pytest.raises(ValueError):
        determinism.canonical_path("case/f.csv")   # relative: discovery's bug, loud


def test_container_inner_path_is_chain_inclusive() -> None:
    assert determinism.container_inner_path(()) is None   # a loose file: no inner_path
    assert (determinism.container_inner_path(("exhibit.zip", "inner/cache.sqlite"))
            == "exhibit.zip!inner/cache.sqlite")
    with pytest.raises(ValueError):
        determinism.container_inner_path(("exhibit.zip", ""))   # collapsible chain


def test_container_join_is_injective_a_bang_in_a_segment_cannot_be_forged() -> None:
    # The forgeable "!" join: archive "a.zip!b" -> member "c" and archive "a.zip" ->
    # member "b!c" must NOT flatten to one inner_path (and one source_file_id). Escaping the
    # within-segment "!" keeps the join reversible, so the two chains stay distinct.
    left_inner = determinism.container_inner_path(("a.zip!b", "c"))
    right_inner = determinism.container_inner_path(("a.zip", "b!c"))
    # Two DIFFERENT inner paths, so two different natural keys and two source rows — the
    # separator is not forgeable into a collision.
    assert left_inner != right_inner


def test_posix_backslash_is_kept_literal_and_does_not_collide_with_slash() -> None:
    # On POSIX a literal "\" is a legal filename byte: /dir/a\b and /dir/a/b are two
    # different files and must derive two source_file_ids (an unconditional \->/ fold collapses
    # them into one — the ID4 collision).
    kept = determinism.canonical_path("/dir/a\\b")
    # A backslash is KEPT, so "/dir/a\\b" and "/dir/a/b" stay two distinct natural keys.
    assert "\\" in kept and kept != "/dir/a/b"


def test_non_ascii_letter_is_not_accepted_as_a_drive_root() -> None:
    # A real drive letter is ASCII a-z/A-Z; a non-ASCII "letter" (Greek capital omega)
    # must not pass as an absolute drive-rooted path.
    with pytest.raises(ValueError):
        determinism.canonical_path("Ω:/x")   # "Ω:/x" — not a genuine drive root


# --- rule 5: canonical JSON -----------------------------------------------------

def test_canonical_json_is_the_single_ids_definition() -> None:
    # Reused, never duplicated: the determinism surface exposes THE ids.py function.
    assert determinism.canonical_json is ids.canonical_json


# --- rule 6: projection aggregation order ---------------------------------------

def test_mentions_order_by_ordinal_then_value_never_rowid() -> None:
    rowid_order = [(2, "bob"), (1, "zoe"), (1, "ann"), (3, None)]
    expected = [(1, "ann"), (1, "zoe"), (2, "bob"), (3, None)]
    assert determinism.ordered_mentions(rowid_order) == expected
    # Insertion (rowid) order is irrelevant: any permutation aggregates identically.
    assert determinism.ordered_mentions(reversed(rowid_order)) == expected


# --- rule 7: the BLOB envelope (R18) ---------------------------------------------

def test_blob_envelope_carries_digest_and_size_never_bytes() -> None:
    payload = b"\x89PNG\r\n\x1a\n-fake-image-bytes"
    envelope = determinism.blob_envelope(payload)
    assert envelope == {"$blob": {"sha256": hashlib.sha256(payload).hexdigest(),
                                  "bytes": len(payload)}}
    # The canonical byte form is stable, so a blob cell hashes identically everywhere.
    encoded = determinism.canonical_json(envelope)
    assert encoded == determinism.canonical_json(determinism.blob_envelope(payload))
    assert encoded.startswith('{"$blob":')


# --- end-to-end: one compact run through the store ------------------------------

def _build_run(store: RunStore, *, input_file_path: str,
               container_chain: tuple[str, ...], fingerprint: str) -> dict[str, str]:
    """One case/run/preset/source/record/observation (+ one edit), paths through rule 4.

    Digests are computed with core.ids exactly as the assembler will. Returns the
    run-carrying ids (the uuid4 roots and the uuid5s derived FROM run_id) so the
    double-run test can mask them — everything else must match byte-for-byte.
    """
    case_id = ids.case_metadata_id()
    run_id = ids.run_id()
    edit_id = ids.edit_log_id()
    store.write_case_metadata(V.CaseMetadata(case_id=case_id,
                                             case_number="GE-2026-04412",
                                             created_at="2026-03-15T08:00:00Z"))
    store.write_run(V.Run(run_id=run_id, case_id=case_id, input_path="/case",
                          started_at="2026-03-15T08:00:01Z", settings_json="{}"))
    preset_id = ids.preset_id("cdr_generic", "1.0")
    store.write_preset(V.Preset(preset_id=preset_id, key="cdr_generic", name="cdr",
                                version="1.0", definition_digest="digest-1"))
    run_preset_id = ids.run_preset_id(run_id, preset_id)
    store.write_run_preset(V.RunPreset(run_preset_id=run_preset_id, run_id=run_id,
                                       preset_id=preset_id))

    path = determinism.canonical_path(input_file_path)
    inner = determinism.container_inner_path(container_chain)
    source_file_id = ids.new_id()
    store.write_source_file(V.SourceFile(source_file_id=source_file_id, run_id=run_id,
                                input_file_path=path, inner_path=inner, format="csv",
                                fingerprint_sha256=fingerprint))
    rr_id = ids.new_id()
    store.write_raw_record(V.RawRecord(raw_record_id=rr_id, source_file_id=source_file_id,
                                       line_no=1,
                                       raw_json='{"a_party":"+41791234567"}'))
    entity_id = ids.new_id()
    store.write_entity(V.Entity(entity_id=entity_id, identifier="+41791234567",
                                raw_identifier="+41 79 123 45 67",
                                identifier_type=V.IdentifierType.MSISDN,
                                type=V.EntityType.DEVICE))
    l1 = ids.l1_projection(
        bearer=None,
        entity=ids.EntityProjection("+41791234567", V.IdentifierType.MSISDN,
                                    V.EntityType.DEVICE),
        spatial=None, temporal=None)
    observation_content_hash, spatiotemporal_entity_hash = ids.digests(fingerprint_sha256=fingerprint,
                                             preset_key="cdr_generic", line_no=1, l1=l1)
    observation_id = ids.new_id()
    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=rr_id,
        preset_id=preset_id, entity_id=entity_id,
        observation_content_hash=observation_content_hash, spatiotemporal_entity_hash=spatiotemporal_entity_hash,
        ))
    store.write_edit_log(V.EditLog(edit_id=edit_id, ordinal=1,
                                   edit_kind=V.EditKind.UPDATE,
                                   edited_at="2026-03-15T09:00:00Z"))
    # EVERY evidence-chain id is masked, not just the run-carrying ones: since decision 50
    # they are all random uuid4. What the dump comparison still proves is that everything
    # DERIVED from content — both digests, every projected value, the whole row shape — is
    # byte-identical across two independent runs. That is the property that matters; the
    # ids were never the evidence.
    return {"case_id": case_id, "run_id": run_id, "edit_id": edit_id,
            "run_preset_id": run_preset_id, "source_file_id": source_file_id,
            "raw_record_id": rr_id, "entity_id": entity_id,
            "observation_id": observation_id}


def test_folder_and_zip_disclosures_share_digests_end_to_end() -> None:
    """Folder==zip parity THROUGH the store: the same bytes disclosed loose and zipped
    persist byte-equal observation digests (no path enters a digest), while the two
    sources stay two distinct rows. Pure-function parity:
    test_ids.py::test_folder_and_zip_of_same_evidence_share_both_digests.
    """
    with RunStore() as folder_store, RunStore() as zip_store:
        _build_run(folder_store, input_file_path="/case/exhibit_A/cdr.csv",
                   container_chain=(), fingerprint="fp-same-bytes")
        _build_run(zip_store, input_file_path="/case/exhibit_A.zip",
                   container_chain=("exhibit_A.zip", "cdr.csv"),
                   fingerprint="fp-same-bytes")
        digest_sql = "SELECT observation_content_hash, spatiotemporal_entity_hash FROM observation"
        assert (folder_store.connection.execute(digest_sql).fetchall()
                == zip_store.connection.execute(digest_sql).fetchall())
        source_sql = "SELECT source_file_id FROM source_file"
        assert (folder_store.connection.execute(source_sql).fetchall()
                != zip_store.connection.execute(source_sql).fetchall())


def _masked_dump(connection: sqlite3.Connection, masks: dict[str, str]) -> str:
    """The full SQL dump with each run-carrying id replaced by its name.

    WHY masking is legitimate: case/run/edit ids are uuid4 BY DESIGN (D10/D28/D34),
    run_preset_id is uuid5 OVER run_id, and since decision 50 every EVIDENCE-CHAIN id is
    uuid4 too. Masking them leaves the digests and every content-derived value unmasked —
    and those must still match byte for byte.
    """
    text = "\n".join(connection.iterdump())
    for name, value in masks.items():
        text = text.replace(value, f"<{name}>")
    return text


def test_double_run_dumps_are_byte_identical_modulo_run_columns() -> None:
    with RunStore() as store_a, RunStore() as store_b:
        masks_a = _build_run(store_a, input_file_path="/case/cdr.csv",
                             container_chain=(), fingerprint="fp")
        masks_b = _build_run(store_b, input_file_path="/case/cdr.csv",
                             container_chain=(), fingerprint="fp")
        dump_a = _masked_dump(store_a.connection, masks_a)
        dump_b = _masked_dump(store_b.connection, masks_b)
    assert dump_a == dump_b
    # Sanity: the compared dump carries real rows (an empty dump would pass trivially).
    assert "INSERT INTO" in dump_a


def test_pipe_in_a_path_survives_canonicalisation_unforged() -> None:
    """Rule 4: a real ``|`` in a path is kept VERBATIM by canonicalisation, never escaped
    into some collapsible form.

    The forgery this used to guard against — two different (path, inner_path) pairs joining
    to one uuid5 input — cannot happen at all since decision 50: a source is found by its
    natural key, and the key is TWO SEPARATE COLUMNS compared column-wise in SQL. There is
    no join, so there is nothing to forge. The property is asserted on the key itself.
    """
    assert "|" in determinism.canonical_path("/evid/a|b")
    left = (determinism.canonical_path("/evid/a|b"),
            determinism.container_inner_path(("c.zip", "f.csv")))
    right = (determinism.canonical_path("/evid/a"),
             determinism.container_inner_path(("b|c.zip", "f.csv")))
    assert left != right