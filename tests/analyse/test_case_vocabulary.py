"""Tests for model_atlas/analyse/case_vocabulary.py (the Verify list).

Covers:     ``read_case_vocabulary`` returns only values actually present, with
            placement counts matching a hand-counted expectation, sorted count-desc/
            value-asc; an entity with no integrated point is absent; a kind with zero
            matching placements yields an empty tuple.
Uses:       ``model_atlas.analyse.helpers`` + the same fixture-building helpers
            as tests/analyse/test_concomitance.py (real layer-typed store writers, real
            ``integrate_spatial``), so the vocabulary's join can never silently diverge
            from what the engine can actually query.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.analyse.helpers import read_case_vocabulary
from model_atlas.analyse.helpers import CriterionKind
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial

KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000


class _Fixture:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.line_no = 0
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="T101a"))
        run_id = ids.run_id()
        store.write_run(V.Run(run_id=run_id, case_id=case_id))
        self.source_id = ids.source_id("/case/t101a.csv", None, None)
        store.write_source(V.Source(source_id=self.source_id, run_id=run_id,
                                    input_file_path="/case/t101a.csv"))
        self.preset_key = "t101a"
        self.preset_id = ids.preset_id(self.preset_key, "1")
        store.write_preset(V.Preset(preset_id=self.preset_id, key=self.preset_key,
                                    name=None, version="1",
                                    definition_digest="sha256-fixture"))

    def add_point(self, *, identifier: str | None, identifier_type: str = "msisdn",
                 entity_type: str = "device", placed: bool = True) -> str:
        """One observation; ``placed=False`` omits the spatial/temporal integration so it
        is NOT a queryable placement (the entity-with-no-integrated-point case)."""
        self.line_no += 1
        record_id = ids.raw_record_id(self.source_id, self.line_no)
        self.store.write_raw_record(V.RawRecord(
            raw_record_id=record_id, source_id=self.source_id,
            line_no=self.line_no, raw_json="{}"))
        natural_key = (record_id, 0)
        entity_id = ids.entity_id(natural_key)
        self.store.write_entity(V.Entity(
            entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
            identifier_type=identifier_type, type=entity_type))
        sx_id = ids.spatial_expression_id(natural_key)
        self.store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx_id, referential="epsg:4326"))
        for ordinal, (part, raw) in enumerate((
                ("latitude", str(KNOWN[0])), ("longitude", str(KNOWN[1])))):
            self.store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.spatial_component_id(sx_id, ordinal),
                spatial_expression_id=sx_id, ordinal=ordinal,
                part=part, raw_value=raw))
        tx_id = ids.temporal_expression_id(natural_key)
        self.store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind="interval"))
        if placed:
            self.store.write_timespan_integration(V.TimespanIntegration(
                timespan_integration_id=ids.timespan_integration_id(tx_id, "fixture/1.0"),
                temporal_expression_id=tx_id, method="fixture/1.0",
                lower_unix_utc_us=BASE_US, upper_unix_utc_us=BASE_US + 60_000_000))
        observation_id = ids.observation_id(record_id, self.preset_key, 0)
        self.store.write_observation(V.Observation(
            observation_id=observation_id, raw_record_id=record_id,
            preset_id=self.preset_id, output_ordinal=0, entity_id=entity_id,
            content_hash=f"ch-{self.line_no}", claim_digest=f"cd-{self.line_no}",
            spatial_expression_id=sx_id,
            temporal_expression_id=tx_id if placed else None,
            activity_role="unknown", spatial_link="at",
            entity_link="self", occupancy="once"))
        return observation_id


def _by_value(entries: tuple, value: str):
    return next(e for e in entries if e.value == value)


def test_vocabulary_reports_only_present_values_with_matching_counts(tmp_path: Path) -> None:
    with RunStore() as store:
        fixture = _Fixture(store)
        fixture.add_point(identifier="+41790000001", identifier_type="msisdn",
                          entity_type="device")
        fixture.add_point(identifier="+41790000002", identifier_type="msisdn",
                          entity_type="device")
        fixture.add_point(identifier="GE123456", identifier_type="plate",
                          entity_type="vehicle")
        # A second placement for the SAME device identifier: its count must be 2.
        fixture.add_point(identifier="+41790000001", identifier_type="msisdn",
                          entity_type="device")
        integrate_spatial(store)
        db_path = tmp_path / "case.matlas.sqlite"
        store.persist(db_path)

    vocab = read_case_vocabulary(db_path)

    entity_types = vocab[CriterionKind.ENTITY_TYPE]
    assert _by_value(entity_types, "device").placement_count == 3
    assert _by_value(entity_types, "vehicle").placement_count == 1
    assert {e.value for e in entity_types} == {"device", "vehicle"}
    assert all(e.qualifier is None for e in entity_types)

    identifier_types = vocab[CriterionKind.IDENTIFIER_TYPE]
    assert _by_value(identifier_types, "msisdn").placement_count == 3
    assert _by_value(identifier_types, "plate").placement_count == 1

    identifiers = vocab[CriterionKind.IDENTIFIER]
    assert _by_value(identifiers, "+41790000001").placement_count == 2
    assert _by_value(identifiers, "+41790000001").qualifier == "msisdn"
    assert _by_value(identifiers, "+41790000002").placement_count == 1
    assert _by_value(identifiers, "GE123456").qualifier == "plate"

    # Deterministic order: count descending, then value ascending.
    assert [e.value for e in identifier_types] == ["msisdn", "plate"]
    assert [e.value for e in identifiers] == [
        "+41790000001", "+41790000002", "GE123456"]


def test_entity_with_no_integrated_point_is_absent(tmp_path: Path) -> None:
    with RunStore() as store:
        fixture = _Fixture(store)
        fixture.add_point(identifier="+41790000001")
        fixture.add_point(identifier="+41790000099", placed=False)   # not queryable
        integrate_spatial(store)
        db_path = tmp_path / "case.matlas.sqlite"
        store.persist(db_path)

    vocab = read_case_vocabulary(db_path)
    identifiers = {e.value for e in vocab[CriterionKind.IDENTIFIER]}
    assert "+41790000001" in identifiers
    assert "+41790000099" not in identifiers


def test_kind_with_no_matching_placements_is_an_empty_tuple(tmp_path: Path) -> None:
    with RunStore() as store:
        fixture = _Fixture(store)
        # A purely typed mention (NULL identifier): the IDENTIFIER kind must be empty,
        # never backfilled from the static core.vocabularies enum.
        fixture.add_point(identifier=None, identifier_type="none", entity_type="camera")
        integrate_spatial(store)
        db_path = tmp_path / "case.matlas.sqlite"
        store.persist(db_path)

    vocab = read_case_vocabulary(db_path)
    assert vocab[CriterionKind.IDENTIFIER] == ()
    assert {e.value for e in vocab[CriterionKind.ENTITY_TYPE]} == {"camera"}
    # No 'vehicle' entity type at all in this case: querying for one is simply absent.
    assert "vehicle" not in {e.value for e in vocab[CriterionKind.ENTITY_TYPE]}
