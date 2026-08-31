"""Tests for model_atlas/integrate/entity.py (the entity-normalisation Verify list).

Covers:     the entity-normalisation design note's Verify bullets (the two Swiss msisdn
            spellings converge and the bare national form refuses; private IP refuses /
            public normalises; two device mentions never get an identifier; "A. Moreau"
            / "moreau a." share a concerns_uid but remain two rows), the full per-kind
            rule table incl. every refuse condition, the digest recomputation (a
            normalised entity's observation digests move AND equal a fresh normative
            recomputation; a refused entity's digests are byte-identical), the
            matlas_meta rule-set stamps, and the report counts.
Uses:       ``model_atlas.integrate.entity``, ``model_atlas.core`` (ids, vocabularies,
            store). Rows are written through the store's own layer-typed writers with
            digests derived exactly as ingest/assemble.py derives them (via core.ids),
            so the pre-normalisation state is the genuine post-assembly shape.
"""
from __future__ import annotations

import pytest

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.entity import (
    NORMALISATION_RULESET,
    STEMMING_RULESET,
    EntityNormaliseReport,
    concerns_uid,
    normalise_entities,
    normalise_identifier,
    stem_concerns,
)

FINGERPRINT = "f" * 64
PRESET_KEY = "test_preset"
PRESET_VERSION = "1"
INPUT_PATH = "/in/exhibit.csv"


# --- the pure rule table (kind -> rule -> refuse conditions) -----------------

@pytest.mark.parametrize(
    ("kind", "raw", "region", "expected"),
    [
        # msisdn -> E.164; country context = leading + / 00 prefix / trunk 0 + region.
        ("msisdn", "+41 79 123 45 67", None, "+41791234567"),
        ("msisdn", "0041791234567", None, "+41791234567"),
        ("msisdn", "0791234567", "CH", "+41791234567"),
        ("msisdn", "(079) 123-45-67", "CH", "+41791234567"),
        ("msisdn", "0791234567", None, None),        # bare national, no region -> refuse
        ("msisdn", "0791234567", "ZZ", None),        # unknown region -> refuse, never guess
        ("msisdn", "41791234567", "CH", None),       # no +/00/trunk-0 -> ambiguous -> refuse
        ("msisdn", "+41x791234567", None, None),     # residue after separators -> refuse
        ("msisdn", "+41791234567890123", None, None),   # 17 digits > E.164 max -> refuse
        # imei -> 14-digit body (check digit / SV digits dropped).
        ("imei", "356938035643809", None, "35693803564380"),
        ("imei", "35-693803-564380-9", None, "35693803564380"),
        ("imei", "35693803564380", None, "35693803564380"),
        ("imei", "3569380356438090", None, "35693803564380"),   # 16-digit IMEISV
        ("imei", "3569380356438", None, None),       # 13 digits -> refuse
        ("imei", "35693803564380x", None, None),     # non-digit residue -> refuse
        # imsi -> global canonical digit string.
        ("imsi", "228 01 1234567890", None, "228011234567890"),
        ("imsi", "22801x", None, None),
        # email -> lowercase; one @, dotted domain.
        ("email", " John.Doe@Example.COM ", None, "john.doe@example.com"),
        ("email", "not-an-email", None, None),
        ("email", "a@b@c.com", None, None),
        ("email", "x@localhost", None, None),        # undotted domain -> refuse
        # plate -> country-qualified, spacing/dash stripped.
        ("plate", "vd 12.345", "CH", "CH:VD12345"),
        ("plate", "FR:AB-123-CD", None, "FR:AB123CD"),
        ("plate", "VD 12345", None, None),           # no country context -> refuse
        # account -> service-qualified only.
        ("account", "Telegram:@x", None, "telegram:@x"),
        ("account", "@x", None, None),               # bare handle -> refuse
        # ip -> public only.
        ("ip", "8.8.8.8", None, "8.8.8.8"),
        ("ip", "10.0.0.5", None, None),              # RFC1918 private -> refuse
        ("ip", "192.168.1.1", None, None),
        ("ip", "100.64.0.5", None, None),            # CGNAT -> refuse
        ("ip", "127.0.0.1", None, None),
        ("ip", "2001:4860:4860:0:0:0:0:8888", None, "2001:4860:4860::8888"),
        ("ip", "fe80::1", None, None),               # link-local -> refuse
        ("ip", "not.an.ip", None, None),
        # mac -> universally-administered unicast only.
        ("mac", "00:1A:2B:3C:4D:5E", None, "00:1a:2b:3c:4d:5e"),
        ("mac", "001A.2B3C.4D5E", None, "00:1a:2b:3c:4d:5e"),
        ("mac", "02:00:00:00:00:01", None, None),    # locally-administered / randomised
        ("mac", "01:00:5E:00:00:01", None, None),    # multicast bit -> refuse
        ("mac", "00:1A:2B:3C:4D", None, None),       # short -> refuse
        # cell -> mcc-mnc-lac-ci, leading zeros significant.
        ("cell", "228-01-1234-5678", None, "228-01-1234-5678"),
        ("cell", "228 01 1234 5678", None, "228-01-1234-5678"),
        ("cell", "228-01-1234", None, None),         # three groups -> refuse
        # camera / case_ref -> register / issuer qualified only.
        ("camera", "CCTV_GE:CAM-042", None, "cctv_ge:CAM-042"),
        ("camera", "CAM-042", None, None),
        ("case_ref", "GE:2026/123", None, "ge:2026/123"),
        ("case_ref", "2026/123", None, None),
        # kinds with no rule are never normalised (purely typed mentions stay NULL).
        ("none", "the device", None, None),
        ("other", "whatever", None, None),
    ],
)
def test_rule_table(kind: str, raw: str, region: str | None, expected: str | None) -> None:
    assert normalise_identifier(kind, raw, default_region=region) == expected


# --- the concerns stemming (decision 22) --------------------------------------

def test_stem_concerns_groups_the_doc_examples() -> None:
    # The decision-22 canonical example: three spellings, one stem, one uid.
    assert stem_concerns("A. Moreau") == "a moreau"
    assert stem_concerns("moreau a.") == "a moreau"
    assert stem_concerns("A.MOREAU") == "a moreau"
    uids = {concerns_uid(text) for text in ("A. Moreau", "moreau a.", "A.MOREAU")}
    assert len(uids) == 1
    # Diacritics are KEPT: merging Müller with Muller would fabricate an attribution.
    assert concerns_uid("Müller") != concerns_uid("Muller")


# --- store fixtures ------------------------------------------------------------

@pytest.fixture()
def store():
    with RunStore() as run_store:
        _seed_l0(run_store)
        yield run_store


def _seed_l0(store: RunStore) -> None:
    """The minimal L0 chain every observation needs (case -> run -> preset -> source)."""
    case_id = ids.case_metadata_id()
    store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="C-1"))
    run_id = ids.run_id()
    store.write_run(V.Run(run_id=run_id, case_id=case_id))
    store.write_preset(V.Preset(
        preset_id=ids.preset_id(PRESET_KEY, PRESET_VERSION),
        key=PRESET_KEY, name=None, version=PRESET_VERSION,
        definition_digest="d" * 64))
    store.write_source(V.Source(
        source_id=ids.source_id(INPUT_PATH, None, None),
        run_id=run_id, input_file_path=INPUT_PATH,
        fingerprint_sha256=FINGERPRINT))
    # Remember the run id for helpers that need it (module-level state avoided).
    store._test_run_id = run_id  # type: ignore[attr-defined]


def _write_mention(
    store: RunStore,
    line_no: int,
    *,
    raw_identifier: str | None,
    identifier_type: str,
    entity_type: str = "unknown",
    concerns: str | None = None,
    with_corners: bool = False,
) -> tuple[str, str]:
    """Write one raw_record + entity + observation exactly as T23 assembles them.

    The digests hash identifier=None (the assembled, pre-normalisation entity corner)
    through the SAME core.ids recipes the assembler uses, so the fixture state is the
    genuine pre-T32 shape. ``with_corners`` adds spatial + temporal + activity corners
    so the digest recomputation must rebuild the full projection, not just the entity
    block. Returns (entity_id, observation_id).
    """
    source_id = ids.source_id(INPUT_PATH, None, None)
    raw_record_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=raw_record_id, source_id=source_id, line_no=line_no,
        raw_json="{}"))
    natural_key = (raw_record_id, PRESET_KEY, 0)
    entity_id = ids.entity_id(natural_key)
    store.write_entity(V.Entity(
        entity_id=entity_id,
        identifier=None,                      # T23 leaves identifier NULL for T32
        raw_identifier=raw_identifier,
        identifier_type=identifier_type,
        type=entity_type,
        concerns=concerns,
        concerns_origin=V.Origin.PRESET_MAPPED if concerns is not None else None,
        concerns_uid=None,
    ))

    spatial_projection = None
    temporal_projection = None
    activity_projection = None
    spatial_expression_id = None
    temporal_expression_id = None
    activity_id = None
    links = ids.ObservationLinks()
    if with_corners:
        spatial_expression_id = ids.spatial_expression_id(natural_key)
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=spatial_expression_id,
            referential="epsg:4326", spatial_source="gnss"))
        spatial_parts = (("latitude", "lat", "46.2030"),
                         ("longitude", "lon", "6.1430"))
        for ordinal, (part, source_field, raw_value) in enumerate(spatial_parts):
            store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.spatial_component_id(spatial_expression_id, ordinal),
                spatial_expression_id=spatial_expression_id, ordinal=ordinal,
                part=part, spatial_role="vicinity",
                source_field=source_field, raw_value=raw_value))
        spatial_projection = ids.SpatialProjection(
            "epsg:4326", "gnss",
            tuple(ids.SpatialComponentProjection(part, "vicinity", raw_value)
                  for part, _, raw_value in spatial_parts))

        temporal_expression_id = ids.temporal_expression_id(natural_key)
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=temporal_expression_id,
            kind="instant", referential="unix_epoch_s"))
        store.write_temporal_component(V.TemporalComponent(
            temporal_component_id=ids.temporal_component_id(temporal_expression_id, 0),
            temporal_expression_id=temporal_expression_id, ordinal=0,
            part="lower", temporal_role="start", stated_offset="+01:00",
            source_field="ts", raw_value="1700000000"))
        temporal_projection = ids.TemporalProjection(
            "instant", "unix_epoch_s",
            (ids.TemporalComponentProjection("lower", "start", "+01:00", "1700000000"),))

        activity_id = ids.activity_id(raw_record_id, 0)
        context = {"callee": "+41791234568"}
        store.write_activity(V.Activity(
            activity_id=activity_id, kind="call",
            context_json=ids.canonical_json(context)))
        store.write_activity_record(V.ActivityRecord(
            activity_record_id=ids.activity_record_id(activity_id, raw_record_id),
            activity_id=activity_id, raw_record_id=raw_record_id))
        activity_projection = ids.ActivityProjection("call", context)
        links = ids.ObservationLinks("subject", "at", "observed_at", "instant")

    l1 = ids.l1_projection(
        entity=ids.EntityProjection(None, identifier_type, entity_type),
        spatial=spatial_projection, temporal=temporal_projection,
        links=links, deleted=None, multiplicity=None,
        activity=activity_projection)
    content_hash, claim_digest = ids.digests(
        fingerprint_sha256=FINGERPRINT, preset_key=PRESET_KEY,
        line_no=line_no, output_ordinal=0, l1=l1)
    observation_id = ids.observation_id(raw_record_id, PRESET_KEY, 0)
    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=raw_record_id,
        preset_id=ids.preset_id(PRESET_KEY, PRESET_VERSION), output_ordinal=0,
        entity_id=entity_id, content_hash=content_hash, claim_digest=claim_digest,
        entity_origin=V.Origin.PRESET_MAPPED,
        activity_id=activity_id,
        spatial_expression_id=spatial_expression_id,
        temporal_expression_id=temporal_expression_id,
        entity_role=links.entity_role,
        entity_position_link=links.entity_position_link,
        entity_time_link=links.entity_time_link,
        spatial_temporal_link=links.spatial_temporal_link))
    return entity_id, observation_id


def _entity_row(store: RunStore, entity_id: str) -> tuple:
    return store.connection.execute(
        "SELECT identifier, identifier_type, raw_identifier, concerns_uid "
        "FROM entity WHERE entity_id = ?", (entity_id,)).fetchone()


def _digests(store: RunStore, observation_id: str) -> tuple:
    return store.connection.execute(
        "SELECT content_hash, claim_digest FROM observation WHERE observation_id = ?",
        (observation_id,)).fetchone()


# --- the T32.md Verify bullets --------------------------------------------------

def test_msisdn_spellings_converge_and_bare_national_refuses(store: RunStore) -> None:
    plus_id, _ = _write_mention(store, 1, raw_identifier="+41 79 123 45 67",
                                identifier_type="msisdn", entity_type="person")
    intl_id, _ = _write_mention(store, 2, raw_identifier="0041791234567",
                                identifier_type="msisdn", entity_type="person")
    bare_id, _ = _write_mention(store, 3, raw_identifier="0791234567",
                                identifier_type="msisdn", entity_type="person")

    # Region CH normalises the two international spellings; the bare national number
    # would need the region too — but this run refuses it in a separate no-region pass?
    # No: ONE pass, region CH — the bare number then normalises as well, so refusal is
    # exercised by running with NO region context here and asserting the two
    # +/00 spellings (which carry their own country context) still normalise.
    report = normalise_entities(store)   # no preset default region

    assert _entity_row(store, plus_id)[0] == "+41791234567"
    assert _entity_row(store, intl_id)[0] == "+41791234567"
    # The bare national form REFUSES: identifier stays NULL, nothing to converge on.
    assert _entity_row(store, bare_id)[0] is None
    assert report.normalised_count == 2
    assert report.refused_count == 1

    # Convergence is a QUERY over non-NULL identifiers — the two spellings group, the
    # refused row is structurally absent, and no rows were merged (still three rows).
    groups = store.connection.execute(
        "SELECT identifier_type, identifier, COUNT(*) FROM entity "
        "WHERE identifier IS NOT NULL GROUP BY identifier_type, identifier").fetchall()
    assert groups == [("msisdn", "+41791234567", 2)]
    assert store.connection.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 3


def test_bare_national_msisdn_normalises_with_region(store: RunStore) -> None:
    bare_id, _ = _write_mention(store, 1, raw_identifier="0791234567",
                                identifier_type="msisdn")
    normalise_entities(store, default_region="CH")
    assert _entity_row(store, bare_id)[0] == "+41791234567"


def test_private_ip_refuses_public_ip_normalises(store: RunStore) -> None:
    private_id, _ = _write_mention(store, 1, raw_identifier="10.0.0.5",
                                   identifier_type="ip")
    public_id, _ = _write_mention(store, 2, raw_identifier="8.8.8.8",
                                  identifier_type="ip")
    report = normalise_entities(store)
    assert _entity_row(store, private_id)[0] is None
    assert _entity_row(store, public_id)[0] == "8.8.8.8"
    assert report.normalised_count == 1
    assert report.refused_count == 1


def test_device_mentions_never_get_an_identifier(store: RunStore) -> None:
    # Two purely typed mentions ('the device'): identifier_type=none, nothing claimed.
    first_id, _ = _write_mention(store, 1, raw_identifier="the device",
                                 identifier_type="none", entity_type="device")
    second_id, _ = _write_mention(store, 2, raw_identifier="the device",
                                  identifier_type="none", entity_type="device")
    report = normalise_entities(store)
    # Neither normalised NOR refused: no rule exists for a typed mention (§2d closed).
    assert report.normalised_count == 0
    assert report.refused_count == 0
    assert _entity_row(store, first_id)[0] is None
    assert _entity_row(store, second_id)[0] is None
    # They stay two distinct rows and can never converge (identifier NULL).
    convergence = store.connection.execute(
        "SELECT COUNT(*) FROM entity WHERE identifier IS NOT NULL").fetchone()[0]
    assert convergence == 0
    assert store.connection.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 2


def test_concerns_uid_groups_without_merging_rows(store: RunStore) -> None:
    first_id, _ = _write_mention(store, 1, raw_identifier="+41791234567",
                                 identifier_type="msisdn", concerns="A. Moreau")
    second_id, _ = _write_mention(store, 2, raw_identifier="+41791234568",
                                  identifier_type="msisdn", concerns="moreau a.")
    report = normalise_entities(store)
    first_uid = _entity_row(store, first_id)[3]
    second_uid = _entity_row(store, second_id)[3]
    assert first_uid is not None
    assert first_uid == second_uid          # one grouping key ...
    assert first_id != second_id            # ... but still two rows (derived, never a key)
    assert report.concerns_count == 2
    grouped = store.connection.execute(
        "SELECT concerns_uid, COUNT(*) FROM entity WHERE concerns_uid IS NOT NULL "
        "GROUP BY concerns_uid").fetchall()
    assert grouped == [(first_uid, 2)]


# --- the digest recomputation ----------------------------------------------------

def test_normalised_entity_digests_recomputed_refused_unchanged(store: RunStore) -> None:
    # Full-corner observations so the recomputation must rebuild the whole projection.
    normalised_id, normalised_observation = _write_mention(
        store, 1, raw_identifier="+41 79 123 45 67", identifier_type="msisdn",
        entity_type="person", concerns="A. Moreau", with_corners=True)
    refused_id, refused_observation = _write_mention(
        store, 2, raw_identifier="0791234567", identifier_type="msisdn",
        entity_type="person", concerns="moreau a.", with_corners=True)
    before_normalised = _digests(store, normalised_observation)
    before_refused = _digests(store, refused_observation)

    report = normalise_entities(store)   # no region: line 2 refuses

    # The normalised entity's digests MOVED (identifier entered the L1 projection) ...
    after_normalised = _digests(store, normalised_observation)
    assert after_normalised[0] != before_normalised[0]
    assert after_normalised[1] != before_normalised[1]
    # ... and equal a fresh, independent recomputation through the normative recipes.
    expected_l1 = ids.l1_projection(
        entity=ids.EntityProjection("+41791234567", "msisdn", "person"),
        spatial=ids.SpatialProjection(
            "epsg:4326", "gnss",
            (ids.SpatialComponentProjection("latitude", "vicinity", "46.2030"),
             ids.SpatialComponentProjection("longitude", "vicinity", "6.1430"))),
        temporal=ids.TemporalProjection(
            "instant", "unix_epoch_s",
            (ids.TemporalComponentProjection("lower", "start", "+01:00", "1700000000"),)),
        links=ids.ObservationLinks("subject", "at", "observed_at", "instant"),
        deleted=None, multiplicity=None,
        activity=ids.ActivityProjection("call", {"callee": "+41791234568"}))
    assert after_normalised == ids.digests(
        fingerprint_sha256=FINGERPRINT, preset_key=PRESET_KEY,
        line_no=1, output_ordinal=0, l1=expected_l1)

    # The refused entity's digests are BYTE-IDENTICAL: refusal changes nothing the
    # projection hashes (identifier stays NULL, the claimed kind is preserved) — even
    # though its concerns_uid WAS derived (excluded from the digests, decision 22).
    assert _digests(store, refused_observation) == before_refused
    assert _entity_row(store, refused_id)[3] is not None      # concerns_uid derived
    assert _entity_row(store, refused_id)[1] == "msisdn"      # the claim survives
    assert report.digests_recomputed_count == 1

    # Evidence text untouched on BOTH rows (derived columns only — no evidence mutation).
    assert _entity_row(store, normalised_id)[2] == "+41 79 123 45 67"
    assert _entity_row(store, refused_id)[2] == "0791234567"


def test_recomputation_handles_cornerless_observations(store: RunStore) -> None:
    # No spatial/temporal/activity corners: the projection's two-empty-slot shape.
    entity_id, observation_id = _write_mention(
        store, 1, raw_identifier="John.Doe@Example.COM", identifier_type="email")
    before = _digests(store, observation_id)
    normalise_entities(store)
    after = _digests(store, observation_id)
    assert after != before
    expected_l1 = ids.l1_projection(
        entity=ids.EntityProjection("john.doe@example.com", "email", "unknown"),
        spatial=None, temporal=None, links=ids.ObservationLinks(),
        deleted=None, multiplicity=None, activity=None)
    assert after == ids.digests(
        fingerprint_sha256=FINGERPRINT, preset_key=PRESET_KEY,
        line_no=1, output_ordinal=0, l1=expected_l1)


# --- rule-set stamps + report -----------------------------------------------------

def test_ruleset_versions_recorded_in_matlas_meta(store: RunStore) -> None:
    _write_mention(store, 1, raw_identifier="8.8.8.8", identifier_type="ip")
    normalise_entities(store)
    row = store.connection.execute(
        "SELECT normalisation_ruleset, stemming_ruleset FROM matlas_meta").fetchone()
    assert row == (NORMALISATION_RULESET, STEMMING_RULESET)
    assert NORMALISATION_RULESET == "entity_norm/1.0"
    assert STEMMING_RULESET == "concerns_stem/1.0"


def test_report_counts(store: RunStore) -> None:
    _write_mention(store, 1, raw_identifier="+41791234567", identifier_type="msisdn",
                   concerns="A. Moreau")                       # normalised + concerns
    _write_mention(store, 2, raw_identifier="10.0.0.5", identifier_type="ip")   # refused
    _write_mention(store, 3, raw_identifier="the device", identifier_type="none",
                   entity_type="device")                       # skipped (no rule)
    _write_mention(store, 4, raw_identifier=None, identifier_type="msisdn")
    # ^ a claimed kind with NO source text: nothing examined -> skipped, not refused
    report = normalise_entities(store)
    assert report == EntityNormaliseReport(
        normalised_count=1, refused_count=1, concerns_count=1,
        digests_recomputed_count=1)
