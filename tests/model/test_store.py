"""Unit tests for the SQLite-backed RunStore projection.

Defines:    tests that to_flat_frame reproduces the flat frame's column order
            (OUTPUT_COLUMNS then the orig_<col> passthrough), disambiguates a
            passthrough name that collides with a canonical column, repeats the
            passthrough on every fanned-out row, drops it on opt-out, and yields an
            empty frame carrying the full canonical column set for an empty store;
            plus Phase-C coverage: write_ranks write-back, persist() to a file, and
            entity deduplication.
Used by:    pytest.
Depends on: model_atlas.model.store, model_atlas.model.schema, model_atlas.model.families.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from model_atlas.model.families import OUTPUT_COLUMNS, Entity, SpatioTemporalAssertion
from model_atlas.model.schema import entity_id_for
from model_atlas.model.store import AssertionRow, RecordRow, RunStore, SourceRow


def _assertion(entity: str) -> SpatioTemporalAssertion:
    return SpatioTemporalAssertion(entity=Entity(entity=entity))


def _record(uid: str, raw: dict[str, object]) -> RecordRow:
    return RecordRow(
        source_record_uid=uid, source_id="s1", line_no=1,
        input_record_id=f"t#{uid}", recovery_state=None, raw=raw,
    )


def test_column_order_is_output_columns_then_passthrough() -> None:
    store = RunStore()
    store.add_source(SourceRow(source_id="s1"))
    store.add_record(_record("r1", {"Lat": "1.5", "City": "Geneva"}))
    store.add_assertion(AssertionRow(
        row_uid="u1", source_record_uid="r1",
        assertion=_assertion("device"), attribution_origin="preset_mapped"))

    frame = store.to_flat_frame(include_source_columns=True, source_column_prefix="orig_")
    # OUTPUT_COLUMNS first, in order, then the passthrough columns in source order.
    assert list(frame.columns) == list(OUTPUT_COLUMNS) + ["orig_Lat", "orig_City"]
    row = frame.iloc[0]
    assert row["entity"] == "device"
    assert row["orig_Lat"] == "1.5" and row["orig_City"] == "Geneva"
    # attribution_origin is stored on the AssertionRow but MUST NOT reach the flat frame.
    assert "attribution_origin" not in frame.columns


def test_passthrough_repeated_on_every_fanned_out_row() -> None:
    # Two assertions of one record repeat that record's passthrough columns on both rows.
    store = RunStore()
    store.add_record(_record("r1", {"City": "Bern"}))
    for uid, ent in (("u1", "a"), ("u2", "b")):
        store.add_assertion(AssertionRow(
            row_uid=uid, source_record_uid="r1",
            assertion=_assertion(ent), attribution_origin="preset_mapped"))

    frame = store.to_flat_frame(include_source_columns=True, source_column_prefix="orig_")
    assert list(frame["entity"]) == ["a", "b"]
    assert list(frame["orig_City"]) == ["Bern", "Bern"]


def test_passthrough_collision_is_disambiguated_with_trailing_underscore() -> None:
    # A passthrough name equal to a canonical column gets a trailing "_" so the verbatim
    # source column survives without clobbering the canonical one.
    store = RunStore()
    store.add_record(_record("r1", {"entity": "raw-entity", "row_uid": "raw-uid"}))
    store.add_assertion(AssertionRow(
        row_uid="u1", source_record_uid="r1",
        assertion=_assertion("device"), attribution_origin="preset_mapped"))

    # An empty prefix forces every source column to collide with its canonical namesake.
    frame = store.to_flat_frame(include_source_columns=True, source_column_prefix="")
    assert list(frame.columns) == list(OUTPUT_COLUMNS) + ["entity_", "row_uid_"]
    row = frame.iloc[0]
    assert row["entity"] == "device"          # canonical, untouched
    assert row["entity_"] == "raw-entity"     # verbatim source column, disambiguated
    assert row["row_uid_"] == "raw-uid"


def test_opt_out_drops_passthrough_columns() -> None:
    store = RunStore()
    store.add_record(_record("r1", {"City": "Oslo"}))
    store.add_assertion(AssertionRow(
        row_uid="u1", source_record_uid="r1",
        assertion=_assertion("device"), attribution_origin="preset_mapped"))

    frame = store.to_flat_frame(include_source_columns=False, source_column_prefix="orig_")
    assert list(frame.columns) == list(OUTPUT_COLUMNS)
    assert not any(c.startswith("orig_") for c in frame.columns)


def test_empty_store_yields_empty_frame_with_full_column_set() -> None:
    store = RunStore()
    frame = store.to_flat_frame(include_source_columns=True, source_column_prefix="orig_")
    assert list(frame.columns) == list(OUTPUT_COLUMNS)   # full canonical set, no passthrough
    assert len(frame) == 0


def test_write_ranks_updates_the_flat_frame() -> None:
    # untangle's result is written back onto the assertion rows and re-emerges in the frame.
    store = RunStore()
    store.add_record(_record("r1", {"City": "Bern"}))
    for uid, ent in (("u1", "a"), ("u2", "b")):
        store.add_assertion(AssertionRow(
            row_uid=uid, source_record_uid="r1",
            assertion=_assertion(ent), attribution_origin="preset_mapped"))

    store.write_ranks([("u1", "main", 1), ("u2", "additional", 2)])
    frame = store.to_flat_frame(include_source_columns=False, source_column_prefix="orig_")
    assert list(frame["record_type"]) == ["main", "additional"]
    assert list(frame["record_rank"]) == [1, 2]


def test_persist_writes_a_queryable_database(tmp_path: Path) -> None:
    store = RunStore()
    store.add_record(_record("r1", {"City": "Oslo"}))
    store.add_assertion(AssertionRow(
        row_uid="u1", source_record_uid="r1",
        assertion=_assertion("device"), attribution_origin="preset_mapped"))

    target = tmp_path / "run.matlas.sqlite"
    target.write_bytes(b"stale")   # a stale file must be replaced, not appended to
    store.persist(target)

    conn = sqlite3.connect(target)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        assert {"assertion", "record", "assertion_flat"} <= tables
        (count,) = conn.execute("SELECT COUNT(*) FROM assertion_flat").fetchone()
        assert count == 1
    finally:
        conn.close()


def test_entities_are_deduplicated() -> None:
    # Two assertions about the same (label, type) share one entity row (Phase-C dedup).
    store = RunStore()
    store.add_record(_record("r1", {"City": "Bern"}))
    for uid in ("u1", "u2"):
        store.add_assertion(AssertionRow(
            row_uid=uid, source_record_uid="r1",
            assertion=_assertion("device"), attribution_origin="preset_mapped"))

    rows = store.connection.execute("SELECT entity_id, label FROM entity").fetchall()
    assert rows == [(entity_id_for("device", None), "device")]
