"""The observation model's acceptance test: can it say what it must say, and nothing else?

Defines:    one parametrised encoding of decision 55's worked cases, each WRITTEN to a real
            store so the schema's link/bearer CHECKs judge it; one test per CHECK proving it
            rejects the malformed row; and the proof that links stay out of the digests.
Used by:    nothing — this is a guard, not a library.
Depends on: ``model_atlas.core.vocabularies`` (the vocabularies under test),
            ``model_atlas.core.store`` (the schema the rows must satisfy) and
            ``model_atlas.core.ids`` (the L1 projection).

WHY this file exists: decision 55 settled the links on a table of real sources (a GNSS fix,
a CDR leg, an alibi, a Waze trace…). If a later edit makes one of them inexpressible — a
CHECK too strict, a member dropped — the enums still build and the DDL still applies; only
these encodings stop being writable. So the table IS the test.
"""
from __future__ import annotations

import sqlite3

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.core.vocabularies import EntityLink, SpatialLink, TemporalLink

_PRESET_KEY = "worked_examples"

# Who holds the position, relative to the targeted entity.
_SELF = "self"        # the bearer IS the targeted entity
_OTHER = "other"      # a second entity row
_NONE = "none"        # no bearer

# (label, entity_link, bearer, has_geometry, spatial_link, temporal_link) — decision 55's
# worked cases, verbatim.
WORKED_EXAMPLES = [
    ("iPhone GNSS fix", EntityLink.SUBJECT, _SELF, True, SpatialLink.MEASURED,
     TemporalLink.AT_LEAST_ONCE),
    ("Cellebrite cell tower (fingerprint)", EntityLink.SUBJECT, _SELF, True,
     SpatialLink.MEASURED, TemporalLink.AT_LEAST_ONCE),
    ("IP geolocation", EntityLink.SUBJECT, _SELF, True, SpatialLink.REGISTERED,
     TemporalLink.AT_LEAST_ONCE),
    ("login from that IP", EntityLink.PARTY, _OTHER, False, None, TemporalLink.AT_LEAST_ONCE),
    ("CDR leg", EntityLink.PARTY, _OTHER, True, SpatialLink.SERVED, TemporalLink.AT_LEAST_ONCE),
    ("ANPR read", EntityLink.PARTY, _OTHER, True, SpatialLink.SEEN, TemporalLink.AT_LEAST_ONCE),
    ("ATM withdrawal", EntityLink.PARTY, _OTHER, True, SpatialLink.USED,
     TemporalLink.AT_LEAST_ONCE),
    ("hotel registration", EntityLink.PARTY, _OTHER, True, SpatialLink.STAYED,
     TemporalLink.AT_LEAST_ONCE),
    ("navigation search", EntityLink.PARTY, _OTHER, True, SpatialLink.SEARCHED,
     TemporalLink.AT_LEAST_ONCE),
    ("chat pin received", EntityLink.PARTY, _OTHER, True, SpatialLink.SHARED,
     TemporalLink.AT_LEAST_ONCE),
    ("prefetched cache row", EntityLink.PARTY, _OTHER, True, SpatialLink.CACHED,
     TemporalLink.AT_LEAST_ONCE),
    ("alibi", EntityLink.PARTY, _OTHER, True, SpatialLink.LOCATED, TemporalLink.NEVER),
    ("left home during T", EntityLink.PARTY, _OTHER, True, SpatialLink.LOCATED,
     TemporalLink.NOT_THROUGHOUT),
    ("GNSS track segment", EntityLink.SUBJECT, _SELF, True, SpatialLink.MEASURED,
     TemporalLink.THROUGHOUT),
    ("Waze trace", EntityLink.UNKNOWN, _NONE, True, SpatialLink.UNKNOWN, TemporalLink.UNKNOWN),
    ("BLE proximity", EntityLink.PARTY, _OTHER, False, None, TemporalLink.AT_LEAST_ONCE),
    ("call with no cell info", EntityLink.SUBJECT, _SELF, False, None,
     TemporalLink.AT_LEAST_ONCE),
    ("contact named in a pin", EntityLink.MENTIONED, _NONE, True, SpatialLink.SHARED,
     TemporalLink.AT_LEAST_ONCE),
]


@pytest.fixture()
def store():
    with RunStore() as run_store:
        yield run_store


def _write(store: RunStore, entity_link, bearer: str, has_geometry: bool, spatial_link,
           temporal_link) -> None:
    """One observation with the scaffolding it needs; the schema CHECKs do the judging."""
    if store.connection.execute("SELECT COUNT(*) FROM preset").fetchone()[0] == 0:
        case_id, run_id = ids.case_metadata_id(), ids.run_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="GE-1"))
        store.write_run(V.Run(run_id=run_id, case_id=case_id))
        store.write_preset(V.Preset(preset_id=ids.preset_id(_PRESET_KEY, "1.0"),
                                    key=_PRESET_KEY, name=_PRESET_KEY, version="1.0",
                                    definition_digest="d"))
        store.write_source_file(V.SourceFile(source_file_id="sf", run_id=run_id,
                                             input_file_path="/case/f.csv"))
    raw_record_id = ids.new_id()
    store.write_raw_record(V.RawRecord(raw_record_id=raw_record_id, source_file_id="sf",
                                       line_no=1, raw_json="{}"))

    def _entity() -> str:
        entity_id = ids.new_id()
        store.write_entity(V.Entity(entity_id=entity_id, identifier=None, raw_identifier=None,
                                    identifier_type=V.IdentifierType.NONE,
                                    type=V.EntityType.DEVICE))
        return entity_id

    entity_id = _entity()
    bearer_entity_id = {_SELF: entity_id, _OTHER: _entity() if bearer == _OTHER else None,
                        _NONE: None}[bearer]
    spatial_expression_id = None
    if has_geometry:
        spatial_expression_id = ids.new_id()
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=spatial_expression_id,
            position_type=V.PositionType.COORDINATE))
    store.write_observation(V.Observation(
        observation_id=ids.new_id(), raw_record_id=raw_record_id,
        preset_id=ids.preset_id(_PRESET_KEY, "1.0"), entity_id=entity_id,
        observation_content_hash="ch", spatiotemporal_entity_hash="cd",
        bearer_entity_id=bearer_entity_id, spatial_expression_id=spatial_expression_id,
        entity_link=entity_link, spatial_link=spatial_link, temporal_link=temporal_link))


@pytest.mark.parametrize(
    "label,entity_link,bearer,has_geometry,spatial_link,temporal_link", WORKED_EXAMPLES,
    ids=[e[0] for e in WORKED_EXAMPLES])
def test_every_worked_example_is_writable(store, label, entity_link, bearer, has_geometry,
                                          spatial_link, temporal_link) -> None:
    """Each worked case must satisfy every schema CHECK as encoded in decision 55."""
    _write(store, entity_link, bearer, has_geometry, spatial_link, temporal_link)


@pytest.mark.parametrize("label,entity_link,bearer,has_geometry,spatial_link", [
    # Invariant 1: subject <=> the bearer IS the targeted entity.
    ("subject with another bearer", EntityLink.SUBJECT, _OTHER, True, SpatialLink.MEASURED),
    ("subject with no bearer", EntityLink.SUBJECT, _NONE, True, SpatialLink.MEASURED),
    ("no link but bearer is the entity", None, _SELF, True, SpatialLink.MEASURED),
    # Invariant 2: party => another entity holds the position.
    ("party with no bearer", EntityLink.PARTY, _NONE, True, SpatialLink.SERVED),
    ("party bearing itself", EntityLink.PARTY, _SELF, True, SpatialLink.SERVED),
    # Invariant 3: unknown => no bearer (the placeholder that manufactured presence).
    ("unknown with a bearer", EntityLink.UNKNOWN, _OTHER, True, SpatialLink.UNKNOWN),
    # Invariant 4: no geometry => no spatial link.
    ("spatial link without geometry", EntityLink.PARTY, _OTHER, False, SpatialLink.SERVED),
])
def test_each_link_invariant_is_a_schema_check(store, label, entity_link, bearer,
                                               has_geometry, spatial_link) -> None:
    """Malformed rows are refused by the DATABASE, not only by ingest — the DB is edited
    directly too, and a constraint only ingest honours is one an edit can break."""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _write(store, entity_link, bearer, has_geometry, spatial_link,
               TemporalLink.AT_LEAST_ONCE)


def test_links_never_reach_the_digests() -> None:
    """Links are the preset author's statement, not record content (decision 55): two presets
    disagreeing on a link over the same record must converge on the same hash — so the
    projection does not even accept them."""
    l1 = ids.l1_projection(
        entity=ids.EntityProjection("+41791234567", "msisdn", "device"),
        bearer=None, spatial=None, temporal=None)
    assert "subject" not in l1 and "at_least_once" not in l1

