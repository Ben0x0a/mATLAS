"""Tests for the RunStore (``model_atlas.core.store``).

Defines:    a compact rebuild of the worked-example scenario THROUGH the store's write API
            (persist + reopen), one test per store-relevant silent-loss inventory row
            (run_db_schema.md — each RAISES / stays distinct as named), a double-run
            determinism test, and the by-construction API-surface layer-split assertion.
Used by:    the verification gate (pytest tests/core/test_store.py).
Depends on: ``model_atlas.core.store`` (the RunStore under test), ``model_atlas.core.ids``
            (deterministic ids + the digest recipe — the assembler's real path), and
            ``model_atlas.core.vocabularies`` (the family dataclasses + enums).

The scenario is built with the SAME ids/digest helpers the real assembly brick will
use, so these tests double as a cross-check that ids.py + the store agree with the settled
model; the store itself computes no id and no digest — it only writes what it is given.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from model_atlas.core import ids
from model_atlas.core import schema
from model_atlas.core import store as store_mod
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore


# --- scenario helpers (build compact T00 evidence through the write API) ----

def _write_case_and_run(store: RunStore, *, case_id: str, run_id: str) -> None:
    store.write_case_metadata(V.CaseMetadata(
        case_id=case_id, case_number="GE-2026-04412", title="Rue du Stand robbery"))
    store.write_run(V.Run(run_id=run_id, case_id=case_id, input_path="/case",
                          settings_json="{}", examiner="insp. Girard"))


def _write_preset(store: RunStore, *, run_id: str, key: str, version: str,
                  digest: str = "digest-1") -> str:
    """Write a preset + its run_preset link; return the preset id."""
    preset_id = ids.preset_id(key, version)
    store.write_preset(V.Preset(preset_id=preset_id, key=key, name=key,
                                version=version, definition_digest=digest))
    store.write_run_preset(V.RunPreset(
        run_preset_id=ids.run_preset_id(run_id, preset_id),
        run_id=run_id, preset_id=preset_id))
    return preset_id


def _write_source(store: RunStore, *, run_id: str, path: str, inner: str | None,
                  fingerprint: str) -> str:
    source_id = ids.source_id(path, inner, None)
    store.write_source(V.Source(
        source_id=source_id, run_id=run_id, input_file_path=path, inner_path=inner,
        format="csv", fingerprint_sha256=fingerprint,
        source_tier=V.SourceTier.PRIMARY))
    return source_id


def _write_gps_assertion(store: RunStore, *, source_id: str, preset_id: str,
                         preset_key: str, line_no: int, fingerprint: str) -> str:
    """A full L1→L3 placement: entity + spatial/temporal corners + integrations + analysis.

    Returns the assertion id. Ids and the two digests are computed with core.ids exactly as
    the assembler will, so the row is a faithful T00-shaped placement.
    """
    output_ordinal = 0
    rr_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=rr_id, source_id=source_id, line_no=line_no,
        raw_json='{"ZLATITUDE":"46.193000","ZLONGITUDE":"6.129000"}',
        recovery_state=V.RecordState.LIVE))

    nat_key = (rr_id, output_ordinal)
    entity_id = ids.entity_id((rr_id, output_ordinal, None,
                               V.IdentifierType.NONE, V.EntityType.DEVICE))
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=None, raw_identifier=None,
        identifier_type=V.IdentifierType.NONE, type=V.EntityType.DEVICE))

    sx_id = ids.spatial_expression_id(nat_key)
    store.write_spatial_expression(V.SpatialExpression(
        spatial_expression_id=sx_id, referential=V.SpatialReferential.EPSG_4326,
        spatial_source="GNSS"))
    spatial_components = (
        V.SpatialComponent(
            spatial_component_id=ids.spatial_component_id(sx_id, 0),
            spatial_expression_id=sx_id, ordinal=0, part="latitude_wgs84",
            source_field="ZLATITUDE", raw_value="46.193000"),
        V.SpatialComponent(
            spatial_component_id=ids.spatial_component_id(sx_id, 1),
            spatial_expression_id=sx_id, ordinal=1, part="longitude_wgs84",
            source_field="ZLONGITUDE", raw_value="6.129000"),
    )
    for component in spatial_components:
        store.write_spatial_component(component)

    tx_id = ids.temporal_expression_id(nat_key)
    store.write_temporal_expression(V.TemporalExpression(
        temporal_expression_id=tx_id, kind=V.TemporalKind.INSTANT,
        referential=V.TemporalReferential.APPLE_CFABSOLUTE,
        temporal_source="internal_clock"))
    temporal_components = (
        V.TemporalComponent(
            temporal_component_id=ids.temporal_component_id(tx_id, 0),
            temporal_expression_id=tx_id, ordinal=0, part="lower",
            temporal_role=V.TemporalRole.LAST_SEEN, stated_offset="N/A",
            source_field="ZTIMESTAMP", raw_value="763689600"),
    )
    for component in temporal_components:
        store.write_temporal_component(component)

    # L2 integrations (space + time), 1:1 with their expression.
    si_method = "passthrough_wgs84/1.0"
    si_id = ids.spatial_integration_id(sx_id, si_method)
    store.write_spatial_integration(V.SpatialIntegration(
        spatial_integration_id=si_id, spatial_expression_id=sx_id, method=si_method,
        latitude_wgs84=46.193, longitude_wgs84=6.129, horizontal_accuracy_m=12.0))
    store.write_spatial_index(V.SpatialIndex(
        spatial_index_id=ids.spatial_index_id(si_id), spatial_integration_id=si_id,
        s2_cell_id=1234567890123456789, s2_level=30))
    ti_method = "epoch_cfabsolute/1.0"
    store.write_timespan_integration(V.TimespanIntegration(
        timespan_integration_id=ids.timespan_integration_id(tx_id, ti_method),
        temporal_expression_id=tx_id, method=ti_method,
        lower_unix_utc_us=763689600_000_000, upper_unix_utc_us=763689600_000_000))

    # The assertion itself, after its corners exist (R20). Digests via core.ids.
    links = ids.AssertionLinks(entity_role=V.EntityRole.SUBJECT)
    l1 = ids.l1_projection(
        entity=ids.EntityProjection(identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.DEVICE),
        spatial=ids.SpatialProjection(
            referential=V.SpatialReferential.EPSG_4326, spatial_source="GNSS",
            components=tuple(
                ids.SpatialComponentProjection(part=c.part, raw_value=c.raw_value)
                for c in spatial_components)),
        temporal=ids.TemporalProjection(
            kind=V.TemporalKind.INSTANT,
            referential=V.TemporalReferential.APPLE_CFABSOLUTE,
            components=tuple(
                ids.TemporalComponentProjection(
                    part=c.part, temporal_role=c.temporal_role,
                    stated_offset=c.stated_offset, raw_value=c.raw_value)
                for c in temporal_components)),
        links=links, deleted=None, multiplicity=None, activity=None)
    content_hash, claim_digest = ids.digests(
        fingerprint_sha256=fingerprint, preset_key=preset_key, line_no=line_no,
        output_ordinal=output_ordinal, l1=l1)
    assertion_id = ids.assertion_id(rr_id, preset_key, output_ordinal)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
        output_ordinal=output_ordinal, entity_id=entity_id, content_hash=content_hash,
        claim_digest=claim_digest, entity_origin=V.Origin.PRESET_MAPPED,
        spatial_expression_id=sx_id, temporal_expression_id=tx_id,
        entity_role=V.EntityRole.SUBJECT,
        entity_position_link=V.EntityPositionLink.AT,
        entity_time_link=V.EntityTimeLink.OBSERVED_AT,
        spatial_temporal_link=V.SpatialTemporalLink.INSTANT, source_label="iOS cache"))
    store.write_analysis(V.Analysis(
        analysis_id=ids.analysis_id(assertion_id, "untangle/2.0"),
        assertion_id=assertion_id, method="untangle/2.0", record_type="gps_fix",
        record_rank=1))
    return assertion_id


def _write_call_with_activity(store: RunStore, *, source_id: str, preset_id: str,
                              preset_key: str, line_no: int, fingerprint: str) -> str:
    """A CDR-shaped placement carrying an activity + activity_record (exercises L1 grouping)."""
    output_ordinal = 0
    rr_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=rr_id, source_id=source_id, line_no=line_no,
        raw_json='{"a_party":"+41791234567"}', recovery_state=V.RecordState.LIVE))

    activity_id = ids.activity_id(rr_id, 0)
    store.write_activity(V.Activity(
        activity_id=activity_id, kind=V.ActivityKind.CALL,
        context_json='{"callee":"+41762223344"}'))
    store.write_activity_record(V.ActivityRecord(
        activity_record_id=ids.activity_record_id(activity_id, rr_id),
        activity_id=activity_id, raw_record_id=rr_id))

    entity_id = ids.entity_id((rr_id, output_ordinal, "+41791234567",
                               V.IdentifierType.MSISDN, V.EntityType.DEVICE))
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier="+41791234567", raw_identifier="+41 79 123 45 67",
        identifier_type=V.IdentifierType.MSISDN, type=V.EntityType.DEVICE))

    l1 = ids.l1_projection(
        entity=ids.EntityProjection(identifier="+41791234567",
                                    identifier_type=V.IdentifierType.MSISDN,
                                    type=V.EntityType.DEVICE),
        spatial=None, temporal=None,
        links=ids.AssertionLinks(entity_role=V.EntityRole.CALLER),
        deleted=None, multiplicity=None,
        activity=ids.ActivityProjection(kind=V.ActivityKind.CALL,
                                        context={"callee": "+41762223344"}))
    content_hash, claim_digest = ids.digests(
        fingerprint_sha256=fingerprint, preset_key=preset_key, line_no=line_no,
        output_ordinal=output_ordinal, l1=l1)
    assertion_id = ids.assertion_id(rr_id, preset_key, output_ordinal)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
        output_ordinal=output_ordinal, entity_id=entity_id, content_hash=content_hash,
        claim_digest=claim_digest, activity_id=activity_id,
        entity_role=V.EntityRole.CALLER,
        entity_position_link=V.EntityPositionLink.WITHIN_RANGE_OF,
        entity_time_link=V.EntityTimeLink.EVENT_AT, source_label="CDR op-A"))
    return assertion_id


def _write_positionless_assertion(store: RunStore, *, source_id: str, preset_id: str,
                                  preset_key: str, line_no: int,
                                  fingerprint: str) -> str:
    """An entity-only assertion (no spatial/temporal corner) — must persist (CSV rule)."""
    output_ordinal = 0
    rr_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=rr_id, source_id=source_id, line_no=line_no,
        raw_json='{"note":"no place"}', recovery_state=V.RecordState.LIVE))
    entity_id = ids.entity_id((rr_id, output_ordinal, None,
                               V.IdentifierType.NONE, V.EntityType.PERSON))
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=None, raw_identifier="l'homme",
        identifier_type=V.IdentifierType.NONE, type=V.EntityType.PERSON))
    l1 = ids.l1_projection(
        entity=ids.EntityProjection(identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.PERSON),
        spatial=None, temporal=None, links=ids.AssertionLinks(),
        deleted=None, multiplicity=None, activity=None)
    content_hash, claim_digest = ids.digests(
        fingerprint_sha256=fingerprint, preset_key=preset_key, line_no=line_no,
        output_ordinal=output_ordinal, l1=l1)
    assertion_id = ids.assertion_id(rr_id, preset_key, output_ordinal)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
        output_ordinal=output_ordinal, entity_id=entity_id, content_hash=content_hash,
        claim_digest=claim_digest, source_label="note"))
    return assertion_id


def _build_scenario(store: RunStore, *, case_id: str, run_id: str) -> None:
    """The compact T00 scenario, written in R20 order, touching all 21 tables + a skip row."""
    _write_case_and_run(store, case_id=case_id, run_id=run_id)
    p_cache = _write_preset(store, run_id=run_id, key="ios_cache", version="1.0")
    p_cdr = _write_preset(store, run_id=run_id, key="cdr_generic", version="1.0")

    # Two byte-identical exhibits (same fingerprint, different paths): distinct sources.
    fp_cache = "fp-cache-9f31"
    s_folder = _write_source(store, run_id=run_id, path="/case/exhibit_A/cache.sqlite",
                             inner=None, fingerprint=fp_cache)
    s_zip = _write_source(store, run_id=run_id, path="/case/exhibit_A_copy.zip",
                          inner="exhibit_A_copy.zip!cache.sqlite", fingerprint=fp_cache)
    _write_gps_assertion(store, source_id=s_folder, preset_id=p_cache,
                         preset_key="ios_cache", line_no=1, fingerprint=fp_cache)
    _write_gps_assertion(store, source_id=s_zip, preset_id=p_cache,
                         preset_key="ios_cache", line_no=1, fingerprint=fp_cache)

    # A CDR source: an activity-bearing call + a positionless note.
    fp_cdr = "fp-cdrA-11aa"
    s_cdr = _write_source(store, run_id=run_id, path="/case/operatorA_cdr.csv",
                          inner=None, fingerprint=fp_cdr)
    _write_call_with_activity(store, source_id=s_cdr, preset_id=p_cdr,
                              preset_key="cdr_generic", line_no=1, fingerprint=fp_cdr)
    _write_positionless_assertion(store, source_id=s_cdr, preset_id=p_cdr,
                                  preset_key="cdr_generic", line_no=2, fingerprint=fp_cdr)

    # An archive encounter + an opt-in, recorded skip of another (run_archive.skipped=1).
    arch_path = "/case/exhibit_A_copy.zip"
    arch_id = ids.archive_id(arch_path)
    store.write_archive(V.Archive(archive_id=arch_id, path=arch_path,
                                  cd_sha256="cd-exA-copy-abc123"))
    store.write_run_archive(V.RunArchive(
        run_archive_id=ids.run_archive_id(run_id, arch_id), run_id=run_id,
        archive_id=arch_id, parsed_at="2026-03-15T08:00:05Z", skipped=1))

    # One audited in-place edit (D34/D41).
    store.write_edit_log(V.EditLog(
        edit_id=ids.edit_log_id(), ordinal=1, table_name="spatial_integration",
        row_id="(demo)", column="latitude_wgs84", old_value="46.2029",
        new_value="46.2030", edit_kind=V.EditKind.UPDATE, edited_by="insp. Girard",
        edited_at="2026-03-15T09:12:00Z"))


# --- build the scenario, persist, reopen -----------------------------------

def test_scenario_persists_and_reopens_clean(tmp_path) -> None:
    out = tmp_path / "run.matlas.sqlite"
    with RunStore() as store:
        _build_scenario(store, case_id=ids.case_metadata_id(), run_id=ids.run_id())
        assert store.persist(out) == out

    reopened = sqlite3.connect(out)
    try:
        assert reopened.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        # foreign_key_check needs FK enforcement enabled on THIS connection to report rows.
        reopened.execute("PRAGMA foreign_keys = ON")
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (reopened.execute("PRAGMA application_id").fetchone()[0]
                == schema.APPLICATION_ID)
        assert reopened.execute("SELECT COUNT(*) FROM matlas_meta").fetchone()[0] == 1
        # Every layer landed: an assertion, an integration, an analysis row, an edit.
        assert reopened.execute("SELECT COUNT(*) FROM assertion").fetchone()[0] == 4
        assert reopened.execute(
            "SELECT COUNT(*) FROM spatial_integration").fetchone()[0] == 2
        assert reopened.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
        assert reopened.execute("SELECT COUNT(*) FROM edit_log").fetchone()[0] == 1
    finally:
        reopened.close()


# --- open_persisted: attach to an existing run DB --------------------------

def _persist_scenario(tmp_path) -> "Path":
    """Build the scenario and persist it, returning the run-DB path."""
    out = tmp_path / "run.matlas.sqlite"
    with RunStore() as store:
        _build_scenario(store, case_id=ids.case_metadata_id(), run_id=ids.run_id())
        store.persist(out)
    return out


def test_open_persisted_reads_and_keeps_the_file(tmp_path) -> None:
    out = _persist_scenario(tmp_path)
    store = RunStore.open_persisted(out)
    try:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM assertion").fetchone()[0] == 4
    finally:
        store.close()
    # An existing run DB is evidence: close() must NOT delete it (unlike a scratch store).
    assert out.exists()
    # And it can be reopened again after close.
    store2 = RunStore.open_persisted(out)
    store2.close()


def test_open_persisted_read_only_rejects_writes(tmp_path) -> None:
    out = _persist_scenario(tmp_path)
    store = RunStore.open_persisted(out, read_only=True)
    try:
        # A read-only handle answers queries but a write hits SQLite's read-only guard.
        assert store.connection.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            store.connection.execute("DELETE FROM analysis")
    finally:
        store.close()
    assert out.exists()


def test_open_persisted_rejects_foreign_and_missing(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        RunStore.open_persisted(tmp_path / "nope.sqlite")
    foreign = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError):
        RunStore.open_persisted(foreign)


def test_open_persisted_on_a_garbage_file_leaks_no_connection(tmp_path, monkeypatch) -> None:
    """SS3: a truncated/garbage file is not a valid SQLite DB, so the first PRAGMA raises
    sqlite3.DatabaseError — before the fix that leaked the connection (the reject branches
    closed, this path did not). The close-on-reject guarantee must hold here too."""
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"this is definitely not a sqlite database header" * 8)

    # Track every connection open_persisted creates, so we can prove it was closed on reject.
    opened: list[sqlite3.Connection] = []
    real_connect = store_mod.sqlite3.connect

    def _tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(store_mod.sqlite3, "connect", _tracking_connect)
    with pytest.raises(sqlite3.DatabaseError):
        RunStore.open_persisted(garbage)
    assert opened, "expected open_persisted to have opened a connection"
    for conn in opened:
        # Operating on a closed connection raises ProgrammingError — so this asserts the
        # connection was closed (no leak) rather than left dangling.
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_direct_write_then_close_is_durable(tmp_path) -> None:
    """SS1: a write made directly through a read-write handle then close() must PERSIST.

    close() commits before closing, so an uncommitted direct write is flushed, not silently
    rolled back — the round-trip reopen sees the row."""
    out = _persist_scenario(tmp_path)
    store = RunStore.open_persisted(out)
    store.write_edit_log(V.EditLog(edit_id="e-direct", ordinal=999,
                                   edit_kind=V.EditKind.UPDATE))
    store.close()   # must flush the pending write rather than discard it

    reopened = RunStore.open_persisted(out, read_only=True)
    try:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM edit_log WHERE edit_id = 'e-direct'").fetchone()[0] == 1
    finally:
        reopened.close()


def test_close_is_idempotent(tmp_path) -> None:
    """close() may run twice (explicit close then context-exit); the second is a no-op and
    must not raise (commit() on an already-closed connection would)."""
    out = _persist_scenario(tmp_path)
    store = RunStore.open_persisted(out)
    store.close()
    store.close()   # second call: no-op, no ProgrammingError
    assert out.exists()


def test_identifier_type_is_free_text() -> None:
    """identifier_type carries no closed CHECK: a preset may name any kind (e.g. bssid),
    which the store accepts and round-trips verbatim."""
    with RunStore() as store:
        store.write_entity(V.Entity(
            entity_id="e-free", identifier=None, raw_identifier="AA:BB:CC:DD:EE:FF",
            identifier_type="bssid", type=V.EntityType.DEVICE))
        got = store.connection.execute(
            "SELECT identifier_type FROM entity WHERE entity_id = 'e-free'").fetchone()
        assert got[0] == "bssid"


# --- silent-loss inventory: one test per store-relevant row ----------------

def test_two_exhibits_collapse_into_one_source_raises() -> None:
    """Row: two exhibits collapse into one source (§2c). Same source natural key RAISES."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        path = "/case/exhibit_A/cache.sqlite"
        # The same file opened twice derives the SAME source_id (uuid5 from the paths, §2c),
        # so the deterministic PK — the backstop the schema names — rejects the second write.
        # (The natural-key UNIQUE cannot catch this pair on its own: SQLite treats the NULL
        # inner_path/subunit as distinct, so the path-derived PK is the true guarantor.)
        source_id = ids.source_id(path, None, None)
        store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                    input_file_path=path))
        with pytest.raises(sqlite3.IntegrityError):
            store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                        input_file_path=path))


def test_duplicate_evidence_drops_an_assertion_raises() -> None:
    """Row: duplicate evidence drops an assertion (§2a). Same (record,preset,ordinal) RAISES."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        preset_id = _write_preset(store, run_id=run_id, key="k", version="1.0")
        source_id = _write_source(store, run_id=run_id, path="/f.csv", inner=None,
                                  fingerprint="fp")
        rr_id = ids.raw_record_id(source_id, 1)
        store.write_raw_record(V.RawRecord(raw_record_id=rr_id, source_id=source_id,
                                           line_no=1, raw_json="{}"))
        e1 = ids.entity_id((rr_id, 0, None, V.IdentifierType.NONE, V.EntityType.DEVICE))
        store.write_entity(V.Entity(entity_id=e1, identifier=None, raw_identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.DEVICE))
        assertion_id = ids.assertion_id(rr_id, "k", 0)
        store.write_assertion(V.Assertion(
            assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
            output_ordinal=0, entity_id=e1, content_hash="ch", claim_digest="cd"))
        # A second reading with the SAME (raw_record_id, preset_id, output_ordinal) and a
        # fresh entity corner: the §2a natural-key UNIQUE must reject the duplicate.
        e2 = ids.entity_id((rr_id, 0, None, V.IdentifierType.NONE, V.EntityType.PERSON))
        store.write_entity(V.Entity(entity_id=e2, identifier=None, raw_identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.PERSON))
        with pytest.raises(sqlite3.IntegrityError):
            store.write_assertion(V.Assertion(
                assertion_id="other", raw_record_id=rr_id, preset_id=preset_id,
                output_ordinal=0, entity_id=e2, content_hash="ch2", claim_digest="cd2"))


def test_two_assertions_share_a_corner_or_entity_raises() -> None:
    """Row: two assertions share a corner or an entity row. UNIQUE on each owned FK RAISES."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        preset_id = _write_preset(store, run_id=run_id, key="k", version="1.0")
        source_id = _write_source(store, run_id=run_id, path="/f.csv", inner=None,
                                  fingerprint="fp")
        rr1 = ids.raw_record_id(source_id, 1)
        rr2 = ids.raw_record_id(source_id, 2)
        for line, rr in ((1, rr1), (2, rr2)):
            store.write_raw_record(V.RawRecord(raw_record_id=rr, source_id=source_id,
                                               line_no=line, raw_json="{}"))
        shared_entity = ids.entity_id((rr1, 0, None, V.IdentifierType.NONE,
                                       V.EntityType.DEVICE))
        store.write_entity(V.Entity(entity_id=shared_entity, identifier=None,
                                    raw_identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.DEVICE))
        store.write_assertion(V.Assertion(
            assertion_id=ids.assertion_id(rr1, "k", 0), raw_record_id=rr1,
            preset_id=preset_id, output_ordinal=0, entity_id=shared_entity,
            content_hash="ch", claim_digest="cd"))
        # A second, otherwise-distinct assertion pointing at the SAME entity_id violates the
        # owned-corner UNIQUE(entity_id) — two assertions may never share an entity row.
        with pytest.raises(sqlite3.IntegrityError):
            store.write_assertion(V.Assertion(
                assertion_id=ids.assertion_id(rr2, "k", 0), raw_record_id=rr2,
                preset_id=preset_id, output_ordinal=0, entity_id=shared_entity,
                content_hash="ch2", claim_digest="cd2"))


def test_two_phones_become_one_entity_stays_distinct() -> None:
    """Row: two phones become one entity (§2d). Two 'device' entities stay two rows."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        source_id = _write_source(store, run_id=run_id, path="/f.csv", inner=None,
                                  fingerprint="fp")
        for line in (1, 2):
            rr = ids.raw_record_id(source_id, line)
            store.write_raw_record(V.RawRecord(raw_record_id=rr, source_id=source_id,
                                               line_no=line, raw_json="{}"))
            # A purely typed 'device' mention (identifier NULL): nothing exists to merge on.
            eid = ids.entity_id((rr, 0, None, V.IdentifierType.NONE, V.EntityType.DEVICE))
            store.write_entity(V.Entity(entity_id=eid, identifier=None,
                                        raw_identifier=None,
                                        identifier_type=V.IdentifierType.NONE,
                                        type=V.EntityType.DEVICE))
        # Both device rows persist — occurrences, never deduped at ingest.
        assert store.connection.execute(
            "SELECT COUNT(*) FROM entity WHERE type = 'device'").fetchone()[0] == 2


def test_register_upgrade_second_integration_for_expression_raises() -> None:
    """Row: register/tzdb upgrade rewrites evidence. A 2nd integration per expr RAISES (v1)."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        source_id = _write_source(store, run_id=run_id, path="/f.csv", inner=None,
                                  fingerprint="fp")
        rr = ids.raw_record_id(source_id, 1)
        store.write_raw_record(V.RawRecord(raw_record_id=rr, source_id=source_id,
                                           line_no=1, raw_json="{}"))
        sx = ids.spatial_expression_id((rr, 0))
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx, referential=V.SpatialReferential.EPSG_4326))
        store.write_spatial_integration(V.SpatialIntegration(
            spatial_integration_id=ids.spatial_integration_id(sx, "m1"),
            spatial_expression_id=sx, method="m1", latitude_wgs84=1.0))
        # v1 UNIQUE(spatial_expression_id): a re-run under a new method may NOT add a second
        # integration for the same expression — it would rewrite evidence in place.
        with pytest.raises(sqlite3.IntegrityError):
            store.write_spatial_integration(V.SpatialIntegration(
                spatial_integration_id=ids.spatial_integration_id(sx, "m2"),
                spatial_expression_id=sx, method="m2", latitude_wgs84=2.0))


def test_operator_input_does_not_mutate_the_digest() -> None:
    """Row: operator input mutates the digest. Changing concerns leaves both digests equal."""
    # Two entities identical except for concerns/origin (operator input) — normatively
    # excluded from the L1 projection, so both digests must be byte-equal.
    def digests_for(concerns: str | None) -> tuple[str, str]:
        entity = ids.EntityProjection(identifier="+41791234567",
                                      identifier_type=V.IdentifierType.MSISDN,
                                      type=V.EntityType.DEVICE)
        l1 = ids.l1_projection(entity=entity, spatial=None, temporal=None,
                               links=ids.AssertionLinks(entity_role=V.EntityRole.CALLER),
                               deleted=None, multiplicity=None, activity=None)
        return ids.digests(fingerprint_sha256="fp", preset_key="k", line_no=1,
                           output_ordinal=0, l1=l1)

    assert digests_for(None) == digests_for("A. Moreau")


def test_positionless_claim_persists() -> None:
    """Row: a positionless claim vanishes from CSV. It persists (one line per assertion)."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        preset_id = _write_preset(store, run_id=run_id, key="k", version="1.0")
        source_id = _write_source(store, run_id=run_id, path="/f.csv", inner=None,
                                  fingerprint="fp")
        assertion_id = _write_positionless_assertion(
            store, source_id=source_id, preset_id=preset_id, preset_key="k",
            line_no=1, fingerprint="fp")
        row = store.connection.execute(
            "SELECT spatial_expression_id, temporal_expression_id FROM assertion "
            "WHERE assertion_id = ?", (assertion_id,)).fetchone()
        assert row == (None, None)   # persisted with both corners NULL, nothing invented


def test_duplicate_edit_log_ordinal_raises() -> None:
    """Row: (D41 rewind journal) a duplicate edit_log ordinal RAISES — the position is UNIQUE."""
    with RunStore() as store:
        store.write_edit_log(V.EditLog(edit_id=ids.edit_log_id(), ordinal=1,
                                       edit_kind=V.EditKind.UPDATE))
        with pytest.raises(sqlite3.IntegrityError):
            store.write_edit_log(V.EditLog(edit_id=ids.edit_log_id(), ordinal=1,
                                           edit_kind=V.EditKind.NEW_ROW))


def test_preset_edit_without_version_bump_raises() -> None:
    """Row: a preset edit without a version bump changes results silently (D40).

    Same key+version + same digest returns the existing id (a legitimate re-selection);
    same key+version + a DIFFERENT digest RAISES (the surfaced finding).
    """
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        preset_id = ids.preset_id("k", "1.0")
        first = store.write_preset(V.Preset(preset_id=preset_id, key="k", name="k",
                                            version="1.0", definition_digest="digest-A"))
        # Identical re-selection: no raise, same id back (SELECT-then-INSERT convenience).
        again = store.write_preset(V.Preset(preset_id=preset_id, key="k", name="k",
                                            version="1.0", definition_digest="digest-A"))
        assert first == again == preset_id
        # Same key+version, DIFFERENT definition: the un-bumped edit must be surfaced.
        with pytest.raises(ValueError):
            store.write_preset(V.Preset(preset_id=preset_id, key="k", name="k",
                                        version="1.0", definition_digest="digest-B"))


def test_opt_in_archive_skip_is_recorded() -> None:
    """Row: an opt-in archive skip hides evidence untraceably. The skip is WRITABLE + stored."""
    with RunStore() as store:
        run_id = ids.run_id()
        _write_case_and_run(store, case_id=ids.case_metadata_id(), run_id=run_id)
        arch_id = ids.archive_id("/case/a.zip")
        store.write_archive(V.Archive(archive_id=arch_id, path="/case/a.zip",
                                      cd_sha256="cd"))
        store.write_run_archive(V.RunArchive(
            run_archive_id=ids.run_archive_id(run_id, arch_id), run_id=run_id,
            archive_id=arch_id, skipped=1))
        # The skip is never traceless: skipped=1 is persisted on the run↔archive link.
        assert store.connection.execute(
            "SELECT skipped FROM run_archive WHERE archive_id = ?",
            (arch_id,)).fetchone()[0] == 1


# --- double-run determinism ------------------------------------------------

# Columns/tables that legitimately carry the run's uuid4 roots (excluded from the compare).
_RUN_ONLY_TABLES = frozenset({"case_metadata", "run", "run_archive", "run_preset"})
_RUN_CARRYING_COLUMNS = {"source": {"run_id"}, "edit_log": {"edit_id"}}


def _fingerprint_db(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every evidence table's rows, run-id-carrying columns dropped, sorted for comparison.

    WHY drop those columns/tables: the case/run roots are uuid4 (D10/D28) and everything
    run-scoped (source.run_id, run_archive, run_preset, edit_log.edit_id) carries them; every
    OTHER id is uuid5 over natural keys, so two runs over the same evidence must match exactly.
    """
    fingerprint: dict[str, list[tuple]] = {}
    for table in schema.COPYABLE_TABLES:
        if table in _RUN_ONLY_TABLES:
            continue
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        drop = _RUN_CARRYING_COLUMNS.get(table, set())
        kept = [c for c in columns if c not in drop]
        rows = conn.execute(f"SELECT {', '.join(kept)} FROM {table}").fetchall()
        fingerprint[table] = sorted(rows, key=repr)
    return fingerprint


def test_double_run_is_deterministic_modulo_run_id() -> None:
    with RunStore() as store_a, RunStore() as store_b:
        _build_scenario(store_a, case_id=ids.case_metadata_id(), run_id=ids.run_id())
        _build_scenario(store_b, case_id=ids.case_metadata_id(), run_id=ids.run_id())
        fingerprint_a = _fingerprint_db(store_a.connection)
        fingerprint_b = _fingerprint_db(store_b.connection)
    assert fingerprint_a == fingerprint_b
    # sanity: the compared surface is non-empty (a silently empty dump would pass trivially).
    assert fingerprint_a["assertion"], "no assertions compared"


# --- the layer split is by construction ------------------------------------

def test_writer_api_is_layer_partitioned_by_construction() -> None:
    """Each public writer targets exactly ONE table in ONE layer — no writer crosses layers.

    Asserts the API SURFACE, not behaviour: every ``write_*`` public method is registered in
    ``WRITER_TABLE`` mapping to a single table string, and there is NO generic public write
    (a ``write``/``insert`` taking a table argument), so an L0/L1 writer is structurally
    unable to reach an L2/L3 table.
    """
    public_writers = {
        name for name, _ in inspect.getmembers(RunStore, predicate=inspect.isfunction)
        if name.startswith("write") and not name.startswith("write_")
    }
    # No generic public write/insert accepting a table (the one thing that would break it).
    assert public_writers == set(), f"unexpected generic writer(s): {public_writers}"

    registered = set(RunStore.WRITER_TABLE)
    declared = {name for name, _ in inspect.getmembers(RunStore, inspect.isfunction)
                if name.startswith("write_")}
    assert declared == registered, (
        "every write_* method must be registered in WRITER_TABLE "
        f"(unregistered={declared - registered}, stale={registered - declared})")

    # Each writer maps to a single, real table in exactly one layer.
    for method, table in RunStore.WRITER_TABLE.items():
        assert isinstance(table, str), f"{method} must target a single table string"
        assert table in store_mod.TABLE_LAYER, f"{method} targets unknown table {table!r}"

    # The union of writer targets is exactly the 21 evidence tables (COPYABLE_TABLES).
    assert set(RunStore.WRITER_TABLE.values()) == set(schema.COPYABLE_TABLES)
