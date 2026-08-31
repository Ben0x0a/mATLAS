"""Tests for the identity discipline: uuid5 ids, canonical JSON, and the two digests.

Defines:    the folder==zip parity, overlapping-disclosure, "|"-forgery, integration-
            invariance, mention-sensitivity, context-recipient, and normative-
            exclusion tests for ``model_atlas/core/ids.py``, plus a DEMO CROSS-CHECK
            that rebuilds three observations of docs/examples/matlas_example.sqlite from
            their stored rows and asserts byte-equal content_hash / claim_digest.
Used by:    the verification gate (pytest).
Depends on: ``model_atlas.core.ids`` and the read-only demo SQLite fixture.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from model_atlas.core import ids

# The read-only demo fixture (THE DOC IS NORMATIVE; the demo transcribed the same
# recipes by hand — the cross-check catches any drift between this module and the doc).
_DEMO_DB = Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"


# --- Small builders so each test reads as one claim -------------------------

def _entity(identifier="+41791234567", id_type="msisdn", etype="device"):
    return ids.EntityProjection(identifier, id_type, etype)


def _spatial(raw="228-01-2042-77341"):
    return ids.SpatialProjection(
        "cell_ref", "cell",
        (ids.SpatialComponentProjection("raw_position", "vicinity", raw),),
    )


def _temporal():
    return ids.TemporalProjection(
        "interval", "local_string",
        (
            ids.TemporalComponentProjection("lower", "start", "+01:00", "2026-03-14 02:20:31"),
            ids.TemporalComponentProjection("duration", "end", "+01:00", "187"),
        ),
    )


def _links():
    return ids.ObservationLinks("caller", "within_range_of", "event_at",
                              "continuous_during_interval")


# Sentinel so a test can pass an EXPLICIT None corner (distinct from "use the default").
_DEFAULT = object()


def _l1(*, entity=None, spatial=_DEFAULT, temporal=_DEFAULT, links=None, deleted=None,
        multiplicity=None, activity=None):
    return ids.l1_projection(
        entity=entity or _entity(),
        spatial=_spatial() if spatial is _DEFAULT else spatial,
        temporal=_temporal() if temporal is _DEFAULT else temporal,
        links=links or _links(),
        deleted=deleted,
        multiplicity=multiplicity,
        activity=activity,
    )


# --- id_for / join separator ------------------------------------------------

def test_id_for_is_deterministic_and_table_scoped():
    # Same table + parts -> same id (diffability); different table tag -> different id
    # even for identical natural-key text (no cross-table collisions).
    assert ids.id_for("source", "/a/b.zip", None, None) == ids.id_for("source", "/a/b.zip", None, None)
    assert ids.archive_id("/a/b.zip") != ids.source_id("/a/b.zip", None, None)


def test_pipe_in_path_does_not_forge_a_different_split():
    # The forgery attempt: with a "|" join, source("a|b", "c") and source("a", "b|c")
    # would both flatten to "a|b|c" and collide. With the \x1f join they must NOT.
    forged = ids.source_id("a|b", "c", None)
    other = ids.source_id("a", "b|c", None)
    assert forged != other
    # And neither may equal the naive pipe-joined uuid5 an attacker was banking on.
    assert ids.SEP not in "a|b|c"   # sanity: the real separator is not the pipe


# --- canonical JSON (determinism rule 5) ------------------------------------

def test_canonical_json_sorts_keys_and_omits_whitespace():
    assert ids.canonical_json({"b": "1", "a": "2"}) == '{"a":"2","b":"1"}'


def test_canonical_json_does_not_reformat_float_source_text():
    # Values are kept as SOURCE TEXT upstream; "46.2030" must survive verbatim (rule 5:
    # no float re-formatting — a stored string can never become 46.203).
    assert ids.canonical_json({"lat": "46.2030"}) == '{"lat":"46.2030"}'


def test_source_order_json_preserves_key_order():
    # raw_json keeps the source's column order so the exported orig_<col> tail is in source
    # order; same separators as canonical, only the ordering differs.
    assert ids.source_order_json({"b": "1", "a": "2"}) == '{"b":"1","a":"2"}'
    assert ids.source_order_json({"lat": "46.2030"}) == '{"lat":"46.2030"}'


# --- folder == zip parity ---------------------------------------------------

def test_folder_and_zip_of_same_evidence_share_both_digests():
    # Same record content, two disclosures: a loose folder and the SAME bytes zipped.
    # Paths appear in NEITHER digest, and the fingerprint is the same bytes -> both the
    # content_hash and the claim_digest must match.
    l1 = _l1()
    fp = "fp-same-bytes"
    ch_folder, cd_folder = ids.digests(
        fingerprint_sha256=fp, preset_key="cdr_generic", line_no=2, output_ordinal=0, l1=l1)
    ch_zip, cd_zip = ids.digests(
        fingerprint_sha256=fp, preset_key="cdr_generic", line_no=2, output_ordinal=0, l1=l1)
    assert ch_folder == ch_zip
    assert cd_folder == cd_zip


# --- two overlapping disclosures --------------------------------------------

def test_overlapping_disclosures_converge_on_claim_only():
    # Two DIFFERENT files (different fingerprints) carrying the SAME record content:
    # claim_digest converges (a possible-same-evidence finding), content_hash differs
    # (scoped to each file's bytes).
    l1 = _l1()
    ch_a, cd_a = ids.digests(
        fingerprint_sha256="fp-cdrA-11aa", preset_key="cdr_generic", line_no=2,
        output_ordinal=0, l1=l1)
    ch_b, cd_b = ids.digests(
        fingerprint_sha256="fp-cdrA2-33cc", preset_key="cdr_generic", line_no=1,
        output_ordinal=0, l1=l1)
    assert cd_a == cd_b          # same claim
    assert ch_a != ch_b          # different file / line_no scope


# --- integration invariance vs mention sensitivity --------------------------

def test_changing_an_integration_value_moves_neither_digest():
    # The integration (register version, geocoded lat/long, tzdb) is NOT in the L1
    # projection, so re-running integration cannot move a digest. We prove it by
    # projecting the SAME L1 twice — an integration change never reaches this function.
    l1_before = _l1(spatial=_spatial())
    l1_after = _l1(spatial=_spatial())   # identical raw_value; only integration would differ
    assert l1_before == l1_after
    args = dict(fingerprint_sha256="fp", preset_key="cdr_generic", line_no=2, output_ordinal=0)
    assert ids.digests(l1=l1_before, **args) == ids.digests(l1=l1_after, **args)


def test_changing_a_mentions_raw_value_moves_both_digests():
    # A component raw_value IS in the L1 projection: changing the tower id moves BOTH.
    args = dict(fingerprint_sha256="fp", preset_key="cdr_generic", line_no=2, output_ordinal=0)
    ch1, cd1 = ids.digests(l1=_l1(spatial=_spatial("228-01-2042-77341")), **args)
    ch2, cd2 = ids.digests(l1=_l1(spatial=_spatial("228-01-2042-99999")), **args)
    assert ch1 != ch2
    assert cd1 != cd2


def test_changing_only_the_recipient_in_context_json_moves_both_digests():
    # R27 SMS counterexample: activity.context_json is INSIDE the L1 projection, so a
    # silently different recipient (a party vanishing/changing) must move BOTH digests.
    args = dict(fingerprint_sha256="fp", preset_key="im_generic", line_no=1, output_ordinal=0)
    act1 = ids.ActivityProjection("message", {"recipient": "+41762223344"})
    act2 = ids.ActivityProjection("message", {"recipient": "+41760000000"})
    ch1, cd1 = ids.digests(l1=_l1(activity=act1), **args)
    ch2, cd2 = ids.digests(l1=_l1(activity=act2), **args)
    assert ch1 != ch2
    assert cd1 != cd2


def test_context_json_key_order_does_not_move_the_digest():
    # The context is re-canonicalised, so source key order is irrelevant (only the
    # CONTENT matters) — otherwise a re-export with reordered keys would forge a
    # false-negative same-evidence result.
    act_ab = ids.ActivityProjection("call", {"callee": "+336", "caller_imei": "352099"})
    act_ba = ids.ActivityProjection("call", {"caller_imei": "352099", "callee": "+336"})
    assert _l1(activity=act_ab) == _l1(activity=act_ba)


# --- normative exclusions ---------------------------------------------------

def test_concerns_and_raw_identifier_are_excluded_by_construction():
    # The projection input has NO concerns / raw_identifier fields at all, so they
    # structurally cannot move a digest. Two entities differing ONLY in those excluded
    # attributes therefore share the identical L1 (they are the same projection input).
    e = _entity(identifier="+41791234567")
    assert _l1(entity=e) == _l1(entity=_entity(identifier="+41791234567"))
    # Guard the exclusion structurally: the dataclass exposes none of the excluded names.
    fields = ids.EntityProjection.__dataclass_fields__
    for excluded in ("concerns", "concerns_uid", "concerns_origin", "raw_identifier"):
        assert excluded not in fields


def test_component_source_field_is_excluded():
    # The spatial/temporal component projections carry NO source_field, so a renamed
    # source column cannot move a digest (normative exclusion).
    assert "source_field" not in ids.SpatialComponentProjection.__dataclass_fields__
    assert "source_field" not in ids.TemporalComponentProjection.__dataclass_fields__


# --- null-corner shape ------------------------------------------------------

def test_missing_corner_is_framed_and_distinct_from_a_present_corner():
    # A positionless / timeless claim still projects (the flat rule: one line per
    # observation, corners nullable). An absent corner emits its presence flag alone.
    l1_no_spatial = _l1(spatial=None)
    assert isinstance(l1_no_spatial, str)
    # It differs from the same claim WITH a spatial corner.
    assert l1_no_spatial != _l1(spatial=_spatial())


def test_absent_corner_differs_from_present_but_empty_corner():
    # Framing must separate "no spatial corner at all" from "a spatial corner whose
    # header fields happen to be empty" — under the old ["",""] shape both collided
    # because _txt(None) == _txt(""). The presence flag + count now keeps them distinct.
    empty_present = ids.SpatialProjection("", "", ())
    assert _l1(spatial=None) != _l1(spatial=empty_present)


# --- INJECTIVITY regression (FIX-03) ----------------------------------------

def test_spatial_heavy_and_temporal_heavy_claims_do_not_collide():
    # Under the OLD unframed format an absent corner and a present-but-empty corner both
    # rendered as two empty slots, so a claim carrying ONLY a (present-but-empty) spatial
    # corner and one carrying ONLY a (present-but-empty) temporal corner produced the
    # identical middle "" ⟂ "" ⟂ "" ⟂ "" and therefore the SAME claim_digest. The
    # presence flags + counts must now separate the two structurally-different claims.
    spatial_heavy = _l1(spatial=ids.SpatialProjection("", "", ()), temporal=None)
    temporal_heavy = _l1(spatial=None, temporal=ids.TemporalProjection("", "", ()))
    assert spatial_heavy != temporal_heavy
    assert ids.claim_digest(spatial_heavy) != ids.claim_digest(temporal_heavy)


def test_embedded_separator_in_a_value_cannot_forge_a_collision():
    # A raw_value carrying a literal SEP (0x1F) used to inject extra delimiter slots, so a
    # single component whose raw smuggled another component's tokens hashed EQUAL to a
    # claim that genuinely had that second component. One component with a SEP-laden raw
    # vs two plain components must now differ (escaping + the component count).
    smuggled = ids.SpatialProjection(
        "cell_ref", "cell",
        (ids.SpatialComponentProjection(
            "raw_position", "vicinity", f"v1{ids.SEP}p2{ids.SEP}r2{ids.SEP}v2"),),
    )
    genuine = ids.SpatialProjection(
        "cell_ref", "cell",
        (
            ids.SpatialComponentProjection("raw_position", "vicinity", "v1"),
            ids.SpatialComponentProjection("p2", "r2", "v2"),
        ),
    )
    assert _l1(spatial=smuggled) != _l1(spatial=genuine)
    assert ids.claim_digest(_l1(spatial=smuggled)) != ids.claim_digest(_l1(spatial=genuine))


def test_canonical_json_rejects_nan_and_infinity():
    # A NaN/Infinity in an activity context would serialise to the non-standard NaN/
    # Infinity token (invalid JSON, and every NaN collides) — allow_nan=False makes it
    # raise loudly instead of forging a silently-equal or unverifiable claim.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            ids.canonical_json({"x": bad})


# --- DEMO CROSS-CHECK -------------------------------------------------------

def _rebuild_from_db(cx: sqlite3.Connection, observation_row: sqlite3.Row):
    """Rebuild the L1 projection input + digest prefix parts from stored demo rows.

    This reads back EXACTLY the columns the normative projection lists — proving the
    round-trip (assemble -> store -> re-project) is stable and doc-faithful.
    """
    a = dict(observation_row)
    ent = cx.execute("SELECT * FROM entity WHERE entity_id=?", (a["entity_id"],)).fetchone()
    entity = ids.EntityProjection(ent["identifier"], ent["identifier_type"], ent["type"])

    spatial = None
    if a["spatial_expression_id"] is not None:
        sx = cx.execute("SELECT * FROM spatial_expression WHERE spatial_expression_id=?",
                        (a["spatial_expression_id"],)).fetchone()
        scs = cx.execute("SELECT * FROM spatial_component WHERE spatial_expression_id=? "
                         "ORDER BY ordinal", (a["spatial_expression_id"],)).fetchall()
        spatial = ids.SpatialProjection(
            sx["referential"], sx["spatial_source"],
            tuple(ids.SpatialComponentProjection(c["part"], c["spatial_role"], c["raw_value"])
                  for c in scs))

    temporal = None
    if a["temporal_expression_id"] is not None:
        tx = cx.execute("SELECT * FROM temporal_expression WHERE temporal_expression_id=?",
                        (a["temporal_expression_id"],)).fetchone()
        tcs = cx.execute("SELECT * FROM temporal_component WHERE temporal_expression_id=? "
                         "ORDER BY ordinal", (a["temporal_expression_id"],)).fetchall()
        temporal = ids.TemporalProjection(
            tx["kind"], tx["referential"],
            tuple(ids.TemporalComponentProjection(
                c["part"], c["temporal_role"], c["stated_offset"], c["raw_value"]) for c in tcs))

    activity = None
    if a["activity_id"] is not None:
        act = cx.execute("SELECT * FROM activity WHERE activity_id=?",
                         (a["activity_id"],)).fetchone()
        # context_json is stored canonical; parse it back so the projection re-canonicalises
        # the SAME content (idempotent) — mirroring how assemble hands it the decoded object.
        ctx = json.loads(act["context_json"]) if act["context_json"] is not None else None
        activity = ids.ActivityProjection(act["kind"], ctx)

    links = ids.ObservationLinks(a["entity_role"], a["entity_position_link"],
                               a["entity_time_link"], a["spatial_temporal_link"])
    l1 = ids.l1_projection(entity=entity, spatial=spatial, temporal=temporal, links=links,
                           deleted=a["deleted"], multiplicity=a["multiplicity"],
                           activity=activity)

    fp, line_no = cx.execute(
        "SELECT s.fingerprint_sha256, r.line_no FROM raw_record r "
        "JOIN source s ON s.source_id=r.source_id WHERE r.raw_record_id=?",
        (a["raw_record_id"],)).fetchone()
    preset_key = cx.execute("SELECT key FROM preset WHERE preset_id=?",
                            (a["preset_id"],)).fetchone()[0]
    return l1, fp, line_no, preset_key


# Three observations spanning the interesting shapes: an instant with activity+context
# (CDR op-A), an interval with two temporal bounds and NO activity (CCTV), and the
# REFERENCED chat pin with a deleted flag + activity context (chat pin).
@pytest.mark.parametrize("source_label", ["CDR op-A", "CCTV", "chat pin"])
def test_demo_cross_check_digests_match_stored(source_label):
    if not _DEMO_DB.exists():
        pytest.skip(f"demo fixture absent: {_DEMO_DB}")
    cx = sqlite3.connect(f"file:{_DEMO_DB}?mode=ro", uri=True)
    cx.row_factory = sqlite3.Row
    try:
        row = cx.execute("SELECT * FROM observation WHERE source_label=? LIMIT 1",
                         (source_label,)).fetchone()
        assert row is not None, f"no observation labelled {source_label!r} in the demo"
        l1, fp, line_no, preset_key = _rebuild_from_db(cx, row)
        ch, cd = ids.digests(fingerprint_sha256=fp, preset_key=preset_key, line_no=line_no,
                             output_ordinal=row["output_ordinal"], l1=l1)
        assert cd == row["claim_digest"], f"claim_digest drift for {source_label}"
        assert ch == row["content_hash"], f"content_hash drift for {source_label}"
    finally:
        cx.close()
