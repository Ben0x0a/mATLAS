"""Tests for the evidence-protected edit API (``model_atlas.app.edit_api``).

Defines:    a small persisted-DB fixture (a full L1->L3 GPS placement built through the
            RunStore write API, then persisted with VACUUM INTO), and tests for the
            evidence-protection rules (update RAISES on evidence, succeeds + logs on an
            integration table), save_as_new_row logging, the
            persisted-file authentication, and the full-rewind scenario (update ->
            new_row -> update, then rewind restores the exact pre-edit state row-by-row
            and the journal keeps originals + inverses).
Used by:    the verification gate (pytest tests/app/test_edit_api.py).
Depends on: ``model_atlas.app.edit_api`` (under test), ``model_atlas.core.store`` (builds the
            fixture DB), ``model_atlas.core.ids`` / ``vocabularies`` (faithful worked-example
            rows).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from model_atlas.app.edit_api import EditApi
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore


# --- fixture: a persisted run DB with one full GPS placement ---------------

def _build_persisted_db(target: Path) -> dict[str, str]:
    """Write a minimal but FK-complete GPS placement and persist it; return key row ids."""
    case_id = ids.case_metadata_id()
    run = ids.run_id()
    with RunStore() as store:
        store.write_case_metadata(V.CaseMetadata(
            case_id=case_id, case_number="GE-2026-04412", title="edit-api fixture"))
        store.write_run(V.Run(run_id=run, case_id=case_id, input_path="/case",
                              settings_json="{}", examiner="insp. Girard"))
        preset_id = ids.preset_id("gps", "1")
        store.write_preset(V.Preset(preset_id=preset_id, key="gps", name="gps",
                                    version="1", definition_digest="digest-1"))
        store.write_run_preset(V.RunPreset(
            run_preset_id=ids.run_preset_id(run, preset_id), run_id=run,
            preset_id=preset_id))
        source_id = ids.source_id("/case/fix.csv", None, None)
        store.write_source(V.Source(
            source_id=source_id, run_id=run, input_file_path="/case/fix.csv",
            format="csv", fingerprint_sha256="fp-1", source_tier=V.SourceTier.PRIMARY))

        line_no, output_ordinal = 1, 0
        rr_id = ids.raw_record_id(source_id, line_no)
        store.write_raw_record(V.RawRecord(
            raw_record_id=rr_id, source_id=source_id, line_no=line_no,
            raw_json='{"lat":"46.193000","lon":"6.129000"}',
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
        store.write_spatial_component(V.SpatialComponent(
            spatial_component_id=ids.spatial_component_id(sx_id, 0),
            spatial_expression_id=sx_id, ordinal=0, part="latitude_wgs84",
            source_field="lat", raw_value="46.193000"))

        si_method = "passthrough_wgs84/1.0"
        si_id = ids.spatial_integration_id(sx_id, si_method)
        store.write_spatial_integration(V.SpatialIntegration(
            spatial_integration_id=si_id, spatial_expression_id=sx_id, method=si_method,
            latitude_wgs84=46.193, longitude_wgs84=6.129, horizontal_accuracy_m=12.0))

        # A temporal corner + its integration, so the editable-table tests can exercise
        # timespan_integration (UNIQUE on the owned temporal_expression FK) too.
        tx_id = ids.temporal_expression_id(nat_key)
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind=V.TemporalKind.INSTANT))
        ti_method = "utc_passthrough/1.0"
        ti_id = ids.timespan_integration_id(tx_id, ti_method)
        store.write_timespan_integration(V.TimespanIntegration(
            timespan_integration_id=ti_id, temporal_expression_id=tx_id, method=ti_method,
            lower_unix_utc_us=1_700_000_000_000_000))

        l1 = ids.l1_projection(
            entity=ids.EntityProjection(identifier=None,
                                        identifier_type=V.IdentifierType.NONE,
                                        type=V.EntityType.DEVICE),
            spatial=ids.SpatialProjection(
                referential=V.SpatialReferential.EPSG_4326, spatial_source="GNSS",
                components=(ids.SpatialComponentProjection(
                    part="latitude_wgs84", raw_value="46.193000"),)),
            temporal=None, links=ids.AssertionLinks(entity_role=V.EntityRole.SUBJECT),
            deleted=None, multiplicity=None, activity=None)
        content_hash, claim_digest = ids.digests(
            fingerprint_sha256="fp-1", preset_key="gps", line_no=line_no,
            output_ordinal=output_ordinal, l1=l1)
        assertion_id = ids.assertion_id(rr_id, "gps", output_ordinal)
        store.write_assertion(V.Assertion(
            assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
            output_ordinal=output_ordinal, entity_id=entity_id,
            content_hash=content_hash, claim_digest=claim_digest,
            entity_origin=V.Origin.PRESET_MAPPED, spatial_expression_id=sx_id,
            temporal_expression_id=tx_id, entity_role=V.EntityRole.SUBJECT))

        # An analysis verdict, so the editable-table tests can exercise analysis'
        # UNIQUE(assertion_id, method) — the composite natural key with a non-FK discriminator.
        an_method = "untangle/1.0"
        an_id = ids.analysis_id(assertion_id, an_method)
        store.write_analysis(V.Analysis(
            analysis_id=an_id, assertion_id=assertion_id, method=an_method,
            record_type="gps_fix", record_rank=1))
        store.persist(target)
    return {"entity_id": entity_id, "spatial_integration_id": si_id,
            "spatial_expression_id": sx_id, "timespan_integration_id": ti_id,
            "temporal_expression_id": tx_id, "analysis_id": an_id,
            "assertion_id": assertion_id}


@pytest.fixture
def persisted_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    target = tmp_path / "case.matlas.sqlite"
    keys = _build_persisted_db(target)
    return target, keys


# --- authentication --------------------------------------------------------

def test_open_rejects_non_matlas_db(tmp_path: Path) -> None:
    import sqlite3
    foreign = tmp_path / "foreign.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="not a mATLAS run database"):
        EditApi.open(foreign)


def test_open_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EditApi.open(tmp_path / "nope.sqlite")


# --- D34 protection --------------------------------------------------------

@pytest.mark.parametrize("table", ["assertion", "entity", "spatial_component"])
def test_update_raises_on_evidence(persisted_db, table) -> None:
    path, keys = persisted_db
    with EditApi.open(path, who="tester") as api:
        row = api.rows(table)[0]
        pk = api._pk(table)
        with pytest.raises(PermissionError, match="save_as_new_row"):
            api.update(table, row[pk], "raw_value" if table == "spatial_component"
                       else "source_label", "x")
        # Nothing was logged — the protection fires before any write.
        assert api.edit_log() == []


def test_update_integration_succeeds_and_logs(persisted_db) -> None:
    path, keys = persisted_db
    si_id = keys["spatial_integration_id"]
    with EditApi.open(path, who="tester") as api:
        api.update("spatial_integration", si_id, "latitude_wgs84", 46.20)
        assert api.row("spatial_integration", si_id)["latitude_wgs84"] == 46.20
        log = api.edit_log()
        assert len(log) == 1
        entry = log[0]
        assert entry["edit_kind"] == V.EditKind.UPDATE.value
        assert entry["table_name"] == "spatial_integration"
        assert entry["column"] == "latitude_wgs84"
        assert entry["old_value"] == "46.193"
        assert entry["new_value"] == "46.2"
        assert entry["edited_by"] == "tester"
        assert entry["ordinal"] == 1
    # Re-open: the change and the audit entry survived the round-trip to disk.
    with EditApi.open(path) as api:
        assert api.row("spatial_integration", si_id)["latitude_wgs84"] == 46.20
        assert len(api.edit_log()) == 1


def test_save_as_new_row_keeps_original_and_logs(persisted_db) -> None:
    path, keys = persisted_db
    entity_id = keys["entity_id"]
    with EditApi.open(path, who="tester") as api:
        before = api.row("entity", entity_id)
        new_id = api.save_as_new_row("entity", entity_id,
                                     {"identifier": "+41791234567"})
        assert new_id != entity_id
        # The original is untouched; the new occurrence carries the edit.
        assert api.row("entity", entity_id) == before
        new_row = api.row("entity", new_id)
        assert new_row["identifier"] == "+41791234567"
        assert new_row["type"] == before["type"]
        assert len(api.rows("entity")) == 2
        log = api.edit_log()
        assert len(log) == 1
        assert log[0]["edit_kind"] == V.EditKind.NEW_ROW.value
        assert log[0]["row_id"] == new_id
        assert '"identifier":"+41791234567"' in log[0]["new_value"]


# --- save_as_new_row re-mints UNIQUE keys on the editable tables -----------

@pytest.mark.parametrize(
    "table, key, fk_column, parent_table",
    [
        ("spatial_integration", "spatial_integration_id",
         "spatial_expression_id", "spatial_expression"),
        ("timespan_integration", "timespan_integration_id",
         "temporal_expression_id", "temporal_expression"),
    ],
)
def test_save_as_new_row_mints_fresh_parent_for_owned_corner(
        persisted_db, table, key, fk_column, parent_table) -> None:
    # The 1:1 owned-corner integrations carry UNIQUE(<owned FK>); a verbatim clone would
    # duplicate that FK and raise IntegrityError. save_as_new_row must instead mint a fresh
    # parent occurrence and point the new row at it.
    path, keys = persisted_db
    with EditApi.open(path, who="tester") as api:
        original = api.row(table, keys[key])
        parents_before = len(api.rows(parent_table))
        new_id = api.save_as_new_row(table, keys[key], {})
        assert new_id != keys[key]
        # Original untouched; a genuinely new occurrence added.
        assert api.row(table, keys[key]) == original
        assert len(api.rows(table)) == 2
        new_row = api.row(table, new_id)
        # The owned FK was re-minted to a FRESH parent that actually exists (FK satisfied).
        assert new_row[fk_column] != original[fk_column]
        assert api.row(parent_table, new_row[fk_column]) is not None
        assert len(api.rows(parent_table)) == parents_before + 1
        # Two new_row entries logged (parent first, then the child).
        kinds = [e["edit_kind"] for e in api.edit_log()]
        assert kinds == [V.EditKind.NEW_ROW.value, V.EditKind.NEW_ROW.value]


def test_save_as_new_row_regenerates_non_fk_discriminator(persisted_db) -> None:
    # analysis' UNIQUE(assertion_id, method) has a non-FK discriminator: save_as_new_row keeps
    # the assertion FK (the verdict is about the same assertion) and regenerates ``method`` so
    # the natural key differs — no IntegrityError, no fabricated parent assertion.
    path, keys = persisted_db
    with EditApi.open(path, who="tester") as api:
        original = api.row("analysis", keys["analysis_id"])
        assertions_before = len(api.rows("assertion"))
        new_id = api.save_as_new_row("analysis", keys["analysis_id"], {})
        assert new_id != keys["analysis_id"]
        assert api.row("analysis", keys["analysis_id"]) == original
        new_row = api.row("analysis", new_id)
        assert new_row["assertion_id"] == original["assertion_id"]   # same real parent
        assert new_row["method"] != original["method"]               # fresh discriminator
        assert len(api.rows("assertion")) == assertions_before       # no forged assertion
        log = api.edit_log()
        assert len(log) == 1 and log[0]["edit_kind"] == V.EditKind.NEW_ROW.value


# --- there is deliberately no empty/blank-row capability -------------------

def test_edit_api_has_no_blank_row_capability(persisted_db) -> None:
    # A fabricated blank row would forge evidence, so the API exposes no way to create one:
    # the only creation path is save_as_new_row (clone an existing occurrence).
    path, keys = persisted_db
    with EditApi.open(path, who="tester") as api:
        assert not hasattr(api, "add_empty_row")
        assert V.EditKind.__members__ == {"UPDATE": V.EditKind.UPDATE,
                                          "NEW_ROW": V.EditKind.NEW_ROW}


# --- D41 full rewind -------------------------------------------------------

def test_full_rewind_restores_state_and_keeps_journal(persisted_db) -> None:
    path, keys = persisted_db
    si_id = keys["spatial_integration_id"]
    entity_id = keys["entity_id"]
    with EditApi.open(path, who="tester") as api:
        # Capture the exact pre-edit state of every table for a row-by-row comparison.
        before = {t: api.rows(t) for t in api.tables()}

        # ordinal 1: update an integration coordinate.
        api.update("spatial_integration", si_id, "longitude_wgs84", 7.0)
        # ordinal 2: mint a new entity occurrence.
        new_id = api.save_as_new_row("entity", entity_id, {"identifier": "clone"})
        # ordinal 3: update the same integration column again.
        api.update("spatial_integration", si_id, "longitude_wgs84", 8.5)

        assert [e["ordinal"] for e in api.edit_log()] == [1, 2, 3]
        assert api.row("spatial_integration", si_id)["longitude_wgs84"] == 8.5
        assert api.row("entity", new_id) is not None

        # Rewind to just after the first edit: undoes ordinals 3 then 2.
        api.rewind(to_ordinal=1)

        # State: ordinal-1 edit stands (7.0), the new row is gone, everything else original.
        assert api.row("spatial_integration", si_id)["longitude_wgs84"] == 7.0
        assert api.row("entity", new_id) is None
        after_entity = api.rows("entity")
        assert after_entity == before["entity"]   # exact pre-edit entity rows restored

        # Journal: originals (1,2,3) kept AND two inverse entries appended (never erased).
        log = api.edit_log()
        assert [e["ordinal"] for e in log] == [1, 2, 3, 4, 5]
        # ordinal 4 = inverse of the ordinal-3 update (8.5 -> 7.0, old/new swapped).
        assert log[3]["edit_kind"] == V.EditKind.UPDATE.value
        assert log[3]["column"] == "longitude_wgs84"
        assert log[3]["old_value"] == "8.5" and log[3]["new_value"] == "7.0"
        # ordinal 5 = inverse of the ordinal-2 new_row (row removed: snapshot -> NULL).
        assert log[4]["edit_kind"] == V.EditKind.NEW_ROW.value
        assert log[4]["row_id"] == new_id
        assert log[4]["old_value"] is not None and log[4]["new_value"] is None

        # Rewind fully to the empty journal position: the ordinal-1 edit is undone too.
        api.rewind(to_ordinal=0)
        assert api.rows("spatial_integration") == before["spatial_integration"]
