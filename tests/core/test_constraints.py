"""The silent-loss inventory (run_db_schema.md) — one named test per row.

Defines:    thirteen tests, one per row of the schema doc's "Silent-loss inventory"
            table, in the doc's row order. Each asserts the forbidden operation RAISES
            or that the distinction SURVIVES, exercising an angle the sibling suites do
            not (identity/digest structure here vs the store's UNIQUE backstops there);
            where a row's primary behavioural guarantee already lives in
            tests/core/test_store.py or tests/core/test_ids.py, the docstring names
            that sibling test — so the row -> test mapping is complete and auditable
            from this file alone.
Used by:    the T15 verification gate (pytest tests/core/test_constraints.py).
Depends on: ``model_atlas.core.ids`` / ``vocabularies`` / ``store`` (the surfaces under
            test) and pytest.

Row -> test map (sibling coverage in [brackets]):
 1 two exhibits collapse into one source (§2c)
     -> test_two_exhibits_never_collapse_into_one_source
        [test_store.py::test_two_exhibits_collapse_into_one_source_raises]
 2 duplicate evidence drops an assertion (§2a)
     -> test_duplicate_evidence_surfaces_as_a_finding_never_a_merge
        [test_store.py::test_duplicate_evidence_drops_an_assertion_raises]
 3 two assertions share a corner or an entity row
     -> test_two_assertions_cannot_share_a_spatial_corner
        [test_store.py::test_two_assertions_share_a_corner_or_entity_raises (entity leg)]
 4 two phones become one entity (§2d)
     -> test_two_devices_never_become_one_entity
        [test_store.py::test_two_phones_become_one_entity_stays_distinct]
 5 register/tzdb upgrade rewrites evidence
     -> test_register_upgrade_cannot_rewrite_evidence
        [test_store.py::test_register_upgrade_second_integration_for_expression_raises,
         test_ids.py::test_changing_an_integration_value_moves_neither_digest]
 6 operator input mutates the digest
     -> test_operator_input_cannot_reach_a_digest
        [test_store.py::test_operator_input_does_not_mutate_the_digest,
         test_ids.py::test_concerns_and_raw_identifier_are_excluded_by_construction]
 7 a context party silently vanishes
     -> test_a_context_party_cannot_silently_vanish
        [test_ids.py::test_changing_only_the_recipient_in_context_json_moves_both_digests]
 8 an activity attaches to the wrong record
     -> test_an_activity_attaches_only_to_its_own_record            (primary home)
 9 a positionless claim vanishes from CSV
     -> test_a_positionless_claim_survives_projection
        [test_store.py::test_positionless_claim_persists]
10 merging two run DBs collides ids
     -> test_merging_two_run_dbs_collides_loudly_never_silently     (primary home)
11 `|` in a path forges equal uuid5 inputs
     -> test_pipe_in_a_path_cannot_forge_equal_ids
        [test_ids.py::test_pipe_in_path_does_not_forge_a_different_split,
         test_determinism.py::test_pipe_in_a_path_survives_canonicalisation_unforged]
12 an opt-in archive skip hides evidence untraceably
     -> test_an_archive_skip_is_never_traceless
        [test_store.py::test_opt_in_archive_skip_is_recorded]
13 a preset edit without a version bump changes results silently
     -> test_a_preset_edit_without_version_bump_is_surfaced
        [test_store.py::test_preset_edit_without_version_bump_raises]
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore

_PRESET_KEY = "cdr_generic"


# --- scenario helpers (minimal R20-ordered seed for the store-driven rows) --

def _seed(store: RunStore) -> tuple[str, str, str]:
    """case -> run -> preset(+link) -> source, in R20 order.

    Returns (run_id, preset_id, source_id). Deliberately minimal: each test writes
    only the rows its inventory row needs on top of this.
    """
    case_id = ids.case_metadata_id()
    run_id = ids.run_id()
    store.write_case_metadata(V.CaseMetadata(case_id=case_id,
                                             case_number="GE-2026-04412"))
    store.write_run(V.Run(run_id=run_id, case_id=case_id, input_path="/case",
                          settings_json="{}"))
    preset_id = ids.preset_id(_PRESET_KEY, "1.0")
    store.write_preset(V.Preset(preset_id=preset_id, key=_PRESET_KEY, name=_PRESET_KEY,
                                version="1.0", definition_digest="digest-1"))
    store.write_run_preset(V.RunPreset(
        run_preset_id=ids.run_preset_id(run_id, preset_id),
        run_id=run_id, preset_id=preset_id))
    source_id = ids.source_id("/case/f.csv", None, None)
    store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                input_file_path="/case/f.csv",
                                fingerprint_sha256="fp"))
    return run_id, preset_id, source_id


def _record(store: RunStore, source_id: str, line_no: int) -> str:
    rr_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(raw_record_id=rr_id, source_id=source_id,
                                       line_no=line_no, raw_json="{}"))
    return rr_id


def _entity(store: RunStore, rr_id: str) -> str:
    entity_id = ids.entity_id((rr_id, 0, None, V.IdentifierType.NONE,
                               V.EntityType.DEVICE))
    store.write_entity(V.Entity(entity_id=entity_id, identifier=None,
                                raw_identifier=None,
                                identifier_type=V.IdentifierType.NONE,
                                type=V.EntityType.DEVICE))
    return entity_id


def _assertion(store: RunStore, rr_id: str, preset_id: str, entity_id: str, *,
               content_hash: str = "ch", claim_digest: str = "cd",
               spatial_expression_id: str | None = None) -> str:
    assertion_id = ids.assertion_id(rr_id, _PRESET_KEY, 0)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
        output_ordinal=0, entity_id=entity_id, content_hash=content_hash,
        claim_digest=claim_digest, spatial_expression_id=spatial_expression_id))
    return assertion_id


# --- row 1 -------------------------------------------------------------------

def test_two_exhibits_never_collapse_into_one_source() -> None:
    """Row 1: two exhibits collapse into one source (§2c).

    Angle here: the source IDENTITY is structurally path-only — ``ids.source_id``
    accepts exactly the three path parts and NO fingerprint parameter, so two
    byte-identical exhibits at two paths cannot derive one id even in principle.
    Store backstop (same natural key raises):
    test_store.py::test_two_exhibits_collapse_into_one_source_raises.
    """
    assert list(inspect.signature(ids.source_id).parameters) == [
        "input_file_path", "inner_path", "subunit"]
    # Same bytes (same fingerprint, irrelevant to identity), two paths: two ids, always.
    assert (ids.source_id("/case/exhibit_A/cache.sqlite", None, None)
            != ids.source_id("/case/exhibit_B/cache.sqlite", None, None))


# --- row 2 -------------------------------------------------------------------

def test_duplicate_evidence_surfaces_as_a_finding_never_a_merge() -> None:
    """Row 2: duplicate evidence drops an assertion (§2a).

    Angle here: the digest is NEVER a key — two assertions carrying the SAME
    claim_digest both persist, and the duplicate SURFACES through the
    ``possible_same_evidence`` finding view instead of merging. The natural-key raise
    is test_store.py::test_duplicate_evidence_drops_an_assertion_raises.
    """
    with RunStore() as store:
        _, preset_id, source_id = _seed(store)
        for line_no in (1, 2):
            rr_id = _record(store, source_id, line_no)
            _assertion(store, rr_id, preset_id, _entity(store, rr_id),
                       content_hash=f"ch-{line_no}", claim_digest="cd-same")
        assert store.connection.execute(
            "SELECT COUNT(*) FROM assertion").fetchone()[0] == 2  # nothing dropped
        assert store.connection.execute(
            "SELECT claim_digest, copies FROM possible_same_evidence"
        ).fetchall() == [("cd-same", 2)]                          # …and it surfaces


# --- row 3 -------------------------------------------------------------------

def test_two_assertions_cannot_share_a_spatial_corner() -> None:
    """Row 3: two assertions share a corner or an entity row.

    Angle here: the SPATIAL corner (UNIQUE ``assertion.spatial_expression_id``) — the
    shared-ENTITY leg of the same row is
    test_store.py::test_two_assertions_share_a_corner_or_entity_raises.
    """
    with RunStore() as store:
        _, preset_id, source_id = _seed(store)
        rr1 = _record(store, source_id, 1)
        rr2 = _record(store, source_id, 2)
        sx_id = ids.spatial_expression_id((rr1, 0))
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx_id,
            referential=V.SpatialReferential.EPSG_4326))
        _assertion(store, rr1, preset_id, _entity(store, rr1),
                   spatial_expression_id=sx_id)
        entity_2 = _entity(store, rr2)
        with pytest.raises(sqlite3.IntegrityError):
            _assertion(store, rr2, preset_id, entity_2,
                       spatial_expression_id=sx_id)   # a corner is owned 1:1, never shared


# --- row 4 -------------------------------------------------------------------

def test_two_devices_never_become_one_entity() -> None:
    """Row 4: two phones become one entity (§2d).

    Angle here: entity ids are OCCURRENCE keys — IDENTICAL column content ('device',
    identifier NULL) mentioned by two records derives two DIFFERENT ids, so no shared
    identity exists to merge on. The store-side persistence of both rows is
    test_store.py::test_two_phones_become_one_entity_stays_distinct.
    """
    same_content = (None, V.IdentifierType.NONE, V.EntityType.DEVICE)
    assert (ids.entity_id(("record-A", 0, *same_content))
            != ids.entity_id(("record-B", 0, *same_content)))


# --- row 5 -------------------------------------------------------------------

def test_register_upgrade_cannot_rewrite_evidence() -> None:
    """Row 5: register/tzdb upgrade rewrites evidence.

    Angle here: (a) an integration's identity is expression ⟂ METHOD, so an upgraded
    register/tzdb derives a NEW id — an in-place overwrite of the old row is impossible
    by identity; (b) the digest projection dataclasses expose NO integration field, so
    the upgrade cannot reach a digest either. Store backstop (v1 UNIQUE):
    test_store.py::test_register_upgrade_second_integration_for_expression_raises;
    digest invariance:
    test_ids.py::test_changing_an_integration_value_moves_neither_digest.
    """
    assert (ids.spatial_integration_id("sx-1", "cell_register/2025-03")
            != ids.spatial_integration_id("sx-1", "cell_register/2026-01"))
    assert (ids.timespan_integration_id("tx-1", "tz_localize/2025a")
            != ids.timespan_integration_id("tx-1", "tz_localize/2026a"))
    # The projection input carries the source's words ONLY — no method, no derived value.
    assert set(ids.SpatialProjection.__dataclass_fields__) == {
        "referential", "spatial_source", "components"}
    assert set(ids.TemporalProjection.__dataclass_fields__) == {
        "kind", "referential", "components"}


# --- row 6 -------------------------------------------------------------------

def test_operator_input_cannot_reach_a_digest() -> None:
    """Row 6: operator input mutates the digest.

    Angle here: EXACT-surface structural exclusion — the projection entry points expose
    only the doc's normative field lists, so concerns / both origin columns (operator
    input) cannot even be passed in. Behavioural digest equality:
    test_store.py::test_operator_input_does_not_mutate_the_digest; excluded-name spot
    checks: test_ids.py::test_concerns_and_raw_identifier_are_excluded_by_construction.
    """
    assert list(inspect.signature(ids.l1_projection).parameters) == [
        "entity", "spatial", "temporal", "links", "deleted", "multiplicity", "activity"]
    assert set(ids.EntityProjection.__dataclass_fields__) == {
        "identifier", "identifier_type", "type"}
    assert set(ids.AssertionLinks.__dataclass_fields__) == {
        "entity_role", "entity_position_link", "entity_time_link",
        "spatial_temporal_link"}


# --- row 7 -------------------------------------------------------------------

def test_a_context_party_cannot_silently_vanish() -> None:
    """Row 7: a context party silently vanishes.

    Angle here: a party REMOVED from ``activity.context_json`` (not merely changed —
    the changed-value case is test_ids.py::test_changing_only_the_recipient_in_
    context_json_moves_both_digests) moves BOTH digests, so the vanishing is loud.
    """
    def _digests(recipients: list[str]) -> tuple[str, str]:
        l1 = ids.l1_projection(
            entity=ids.EntityProjection("+41791234567", V.IdentifierType.MSISDN,
                                        V.EntityType.DEVICE),
            spatial=None, temporal=None,
            links=ids.AssertionLinks(entity_role=V.EntityRole.SENDER),
            deleted=None, multiplicity=None,
            activity=ids.ActivityProjection(V.ActivityKind.MESSAGE,
                                            {"recipients": recipients}))
        return ids.digests(fingerprint_sha256="fp", preset_key="im_generic",
                           line_no=1, output_ordinal=0, l1=l1)

    both = _digests(["+41761112233", "+41762223344"])
    one = _digests(["+41761112233"])
    assert both[0] != one[0]   # content_hash moves
    assert both[1] != one[1]   # claim_digest moves


# --- row 8 -------------------------------------------------------------------

def test_an_activity_attaches_only_to_its_own_record() -> None:
    """Row 8: an activity attaches to the wrong record. (Primary home of this row.)

    Two guards: the id derivation BINDS an activity to its record (a different record
    derives a different activity id), and UNIQUE(activity_id, raw_record_id) rejects a
    duplicated link row. The "ingest writes exactly one link, its own record" invariant
    (R7) is an assembly-brick lint — enforced there, outside the store's reach.
    """
    assert ids.activity_id("record-A", 0) != ids.activity_id("record-B", 0)
    with RunStore() as store:
        _, _, source_id = _seed(store)
        rr_id = _record(store, source_id, 1)
        activity_id = ids.activity_id(rr_id, 0)
        store.write_activity(V.Activity(activity_id=activity_id,
                                        kind=V.ActivityKind.CALL))
        store.write_activity_record(V.ActivityRecord(
            activity_record_id=ids.activity_record_id(activity_id, rr_id),
            activity_id=activity_id, raw_record_id=rr_id))
        # A second link row for the SAME (activity, record) — a different surrogate id
        # so the natural-key UNIQUE (not the PK) is what rejects it.
        with pytest.raises(sqlite3.IntegrityError):
            store.write_activity_record(V.ActivityRecord(
                activity_record_id="a-second-surrogate",
                activity_id=activity_id, raw_record_id=rr_id))


# --- row 9 -------------------------------------------------------------------

def test_a_positionless_claim_survives_projection() -> None:
    """Row 9: a positionless claim vanishes from CSV.

    Angle here: the digest pipeline — a claim with NO corner and NO activity still
    projects (each missing block contributes exactly its two empty header slots: 15
    slots joined by 14 separators) and yields both digests, so nothing upstream of the
    flat view can drop it. Store persistence (both corner FKs NULL, row kept):
    test_store.py::test_positionless_claim_persists.
    """
    l1 = ids.l1_projection(
        entity=ids.EntityProjection(None, V.IdentifierType.NONE, V.EntityType.PERSON),
        spatial=None, temporal=None, links=ids.AssertionLinks(),
        deleted=None, multiplicity=None, activity=None)
    # 4 entity slots + 2 spatial + 2 temporal + 5 links/deleted/multiplicity + 2 activity.
    assert l1.count(ids.SEP) == 14
    content_hash, claim_digest = ids.digests(fingerprint_sha256="fp", preset_key="k",
                                             line_no=1, output_ordinal=0, l1=l1)
    for digest in (content_hash, claim_digest):
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


# --- row 10 ------------------------------------------------------------------

def test_merging_two_run_dbs_collides_loudly_never_silently(tmp_path) -> None:
    """Row 10: merging two run DBs collides ids. (Primary home of this row.)

    ``run_id`` feeds no child id, so two runs over the same evidence derive EQUAL child
    ids (the diffability promise); the DB FILE is therefore the run scope, and pouring
    one run DB into another collides on the deterministic PKs — loudly, never as a
    silent duplicate. Cross-run aggregation must key by (run_id, *_id) instead.
    """
    with RunStore() as store_a, RunStore() as store_b:
        for store in (store_a, store_b):
            _, preset_id, source_id = _seed(store)
            rr_id = _record(store, source_id, 1)
            _assertion(store, rr_id, preset_id, _entity(store, rr_id))
        select_ids = "SELECT assertion_id FROM assertion ORDER BY assertion_id"
        assert (store_a.connection.execute(select_ids).fetchall()
                == store_b.connection.execute(select_ids).fetchall())  # diffability
        # Simulate the forbidden merge: pour run B's assertions into run A's file.
        other = tmp_path / "run_b.matlas.sqlite"
        store_b.persist(other)
        store_a.connection.execute("ATTACH ? AS other", (str(other),))
        with pytest.raises(sqlite3.IntegrityError):
            store_a.connection.execute(
                "INSERT INTO main.assertion SELECT * FROM other.assertion")


# --- row 11 ------------------------------------------------------------------

def test_pipe_in_a_path_cannot_forge_equal_ids() -> None:
    """Row 11: `|` in a path forges equal uuid5 inputs.

    Angle here: the inner_path/subunit boundary (the sibling
    test_ids.py::test_pipe_in_path_does_not_forge_a_different_split shifts the
    input/inner boundary; test_determinism.py::test_pipe_in_a_path_survives_
    canonicalisation_unforged exercises the canonicalised chain). The separator itself
    is pinned to the unit separator, which no path can contain; the PK collision is the
    doc's recorded backstop.
    """
    assert ids.SEP == "\x1f"
    # With a "|" join both would flatten to "/f.zip|a|b|c" and collide; they must not.
    assert (ids.source_id("/f.zip", "a|b", "c")
            != ids.source_id("/f.zip", "a", "b|c"))


# --- row 12 ------------------------------------------------------------------

def test_an_archive_skip_is_never_traceless() -> None:
    """Row 12: an opt-in archive skip hides evidence untraceably.

    Angle here: the schema forbids both silent shapes — a NULL ``skipped`` (NOT NULL:
    an encounter can never be recorded with an unknown skip state) and a second link
    row for the same (run, archive) (UNIQUE: one encounter, one record). The recorded
    skip write itself is test_store.py::test_opt_in_archive_skip_is_recorded; skip
    ELIGIBILITY (same cd_sha256 + identical preset set, D38/D40) is an ingest-brick
    rule outside the store.
    """
    with RunStore() as store:
        run_id, _, _ = _seed(store)
        arch_id = ids.archive_id("/case/a.zip")
        store.write_archive(V.Archive(archive_id=arch_id, path="/case/a.zip",
                                      cd_sha256="cd"))
        with pytest.raises(sqlite3.IntegrityError):   # unknown skip state: rejected
            store.write_run_archive(V.RunArchive(
                run_archive_id=ids.run_archive_id(run_id, arch_id), run_id=run_id,
                archive_id=arch_id, skipped=None))
        store.write_run_archive(V.RunArchive(
            run_archive_id=ids.run_archive_id(run_id, arch_id), run_id=run_id,
            archive_id=arch_id, skipped=1))
        with pytest.raises(sqlite3.IntegrityError):   # one encounter, one record
            store.write_run_archive(V.RunArchive(
                run_archive_id="another-surrogate", run_id=run_id,
                archive_id=arch_id, skipped=0))


# --- row 13 ------------------------------------------------------------------

def test_a_preset_edit_without_version_bump_is_surfaced() -> None:
    """Row 13: a preset edit without a version bump changes results silently.

    Angle here: identity stability is what makes the D40 finding COMPARABLE —
    ``preset_id`` derives from key ⟂ version alone (the digest is not an input), so an
    un-bumped edit lands on the SAME id across runs and the digest mismatch is
    queryable; a proper bump moves ``preset_id``, while the assertion's natural key
    uses the preset KEY (D27) and so stays version-stable. The raising write is
    test_store.py::test_preset_edit_without_version_bump_raises.
    """
    assert list(inspect.signature(ids.preset_id).parameters) == ["key", "version"]
    assert ids.preset_id(_PRESET_KEY, "1.0") != ids.preset_id(_PRESET_KEY, "1.1")
    # D27: the assertion key carries the preset KEY, not its (version-bumped) id.
    assert "preset_key" in inspect.signature(ids.assertion_id).parameters
