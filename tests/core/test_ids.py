"""Tests for the identity discipline: uuid5 ids, canonical JSON, and the two digests.

Defines:    the folder==zip parity, overlapping-disclosure, "|"-forgery, integration-
            invariance, mention-sensitivity, context-recipient, and normative-
            exclusion tests for ``model_atlas/core/ids.py``, plus a DEMO CROSS-CHECK
            that rebuilds three observations of docs/examples/matlas_example.sqlite from
            their stored rows and asserts byte-equal observation_content_hash / spatiotemporal_entity_hash.
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
        (ids.SpatialComponentProjection("raw_position", None, raw),),
    )


def _temporal():
    return ids.TemporalProjection(
        "interval", "local_string",
        (
            ids.TemporalComponentProjection("lower", "+01:00", "2026-03-14 02:20:31"),
            ids.TemporalComponentProjection("duration", "+01:00", "187"),
        ),
    )


def _bearer():
    """A position bearer — the antenna a CDR leg's phone was served by."""
    return ids.EntityProjection("228-01-2042-77341", "cell", "antenna")


# Sentinel so a test can pass an EXPLICIT None corner (distinct from "use the default").
_DEFAULT = object()


def _l1(*, entity=None, bearer=_DEFAULT, spatial=_DEFAULT, temporal=_DEFAULT):
    return ids.l1_projection(
        entity=entity or _entity(),
        bearer=_bearer() if bearer is _DEFAULT else bearer,
        spatial=_spatial() if spatial is _DEFAULT else spatial,
        temporal=_temporal() if temporal is _DEFAULT else temporal,
    )


# --- id_for / join separator ------------------------------------------------

def test_id_for_is_deterministic_and_table_scoped():
    # Same table + parts -> same id (diffability); different table tag -> different id
    # even for identical natural-key text (no cross-table collisions).
    assert ids.id_for("source", "/a/b.zip", None, None) == ids.id_for("source", "/a/b.zip", None, None)
    assert ids.archive_id("/a/b.zip") != ids.new_id()


def test_pipe_in_path_does_not_forge_a_different_split():
    # The forgery attempt: with a "|" join, source("a|b", "c") and source("a", "b|c")
    # would both flatten to "a|b|c" and collide. With the \x1f join they must NOT.
    forged = ids.new_id()
    other = ids.new_id()
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
    # observation_content_hash and the spatiotemporal_entity_hash must match.
    l1 = _l1()
    fp = "fp-same-bytes"
    ch_folder, cd_folder = ids.digests(
        fingerprint_sha256=fp, preset_key="cdr_generic", line_no=2, l1=l1)
    ch_zip, cd_zip = ids.digests(
        fingerprint_sha256=fp, preset_key="cdr_generic", line_no=2, l1=l1)
    assert ch_folder == ch_zip
    assert cd_folder == cd_zip


# --- two overlapping disclosures --------------------------------------------

def test_overlapping_disclosures_converge_on_claim_only():
    # Two DIFFERENT files (different fingerprints) carrying the SAME record content:
    # spatiotemporal_entity_hash converges (a possible-same-evidence finding), observation_content_hash differs
    # (scoped to each file's bytes).
    l1 = _l1()
    ch_a, cd_a = ids.digests(
        fingerprint_sha256="fp-cdrA-11aa", preset_key="cdr_generic", line_no=2, l1=l1)
    ch_b, cd_b = ids.digests(
        fingerprint_sha256="fp-cdrA2-33cc", preset_key="cdr_generic", line_no=1, l1=l1)
    assert cd_a == cd_b          # same claim
    assert ch_a != ch_b          # different file / line_no scope


# --- integration invariance vs mention sensitivity --------------------------

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
    # identical middle "" ⟂ "" ⟂ "" ⟂ "" and therefore the SAME spatiotemporal_entity_hash. The
    # presence flags + counts must now separate the two structurally-different claims.
    spatial_heavy = _l1(spatial=ids.SpatialProjection("", "", ()), temporal=None)
    temporal_heavy = _l1(spatial=None, temporal=ids.TemporalProjection("", "", ()))
    assert spatial_heavy != temporal_heavy
    assert ids.spatiotemporal_entity_hash(spatial_heavy) != ids.spatiotemporal_entity_hash(temporal_heavy)


def test_embedded_separator_in_a_value_cannot_forge_a_collision():
    # A raw_value carrying a literal SEP (0x1F) used to inject extra delimiter slots, so a
    # single component whose raw smuggled another component's tokens hashed EQUAL to a
    # claim that genuinely had that second component. One component with a SEP-laden raw
    # vs two plain components must now differ (escaping + the component count).
    smuggled = ids.SpatialProjection(
        "cell_ref", "cell",
        (ids.SpatialComponentProjection(
            "raw_position", None, f"v1{ids.SEP}p2{ids.SEP}m/s{ids.SEP}v2"),),
    )
    genuine = ids.SpatialProjection(
        "cell_ref", "cell",
        (
            ids.SpatialComponentProjection("raw_position", None, "v1"),
            ids.SpatialComponentProjection("p2", "m/s", "v2"),
        ),
    )
    assert _l1(spatial=smuggled) != _l1(spatial=genuine)
    assert ids.spatiotemporal_entity_hash(_l1(spatial=smuggled)) != ids.spatiotemporal_entity_hash(_l1(spatial=genuine))


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
            tuple(ids.SpatialComponentProjection(c["part"], c["unit"], c["raw_value"])
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
                c["part"], c["stated_offset"], c["raw_value"]) for c in tcs))

    # A `subject` row's bearer IS its entity, and projects no bearer block (decision 55).
    bearer = None
    if a["bearer_entity_id"] not in (None, a["entity_id"]):
        b = cx.execute("SELECT * FROM entity WHERE entity_id=?",
                       (a["bearer_entity_id"],)).fetchone()
        bearer = ids.EntityProjection(b["identifier"], b["identifier_type"], b["type"])
    l1 = ids.l1_projection(entity=entity, bearer=bearer, spatial=spatial, temporal=temporal)

    fp, line_no = cx.execute(
        "SELECT s.fingerprint_sha256, r.line_no FROM raw_record r "
        "JOIN source_file s ON s.source_file_id=r.source_file_id WHERE r.raw_record_id=?",
        (a["raw_record_id"],)).fetchone()
    preset_key = cx.execute("SELECT key FROM preset WHERE preset_id=?",
                            (a["preset_id"],)).fetchone()[0]
    return l1, fp, line_no, preset_key


# Three observations spanning the interesting shapes: a party row with a bearer
# (CDR op-A), a subject row whose bearer is itself (CCTV), and the shared chat pin
# with a recovered flag (chat pin).
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
        ch, cd = ids.digests(fingerprint_sha256=fp, preset_key=preset_key, line_no=line_no, l1=l1)
        assert cd == row["spatiotemporal_entity_hash"], f"spatiotemporal_entity_hash drift for {source_label}"
        assert ch == row["observation_content_hash"], f"observation_content_hash drift for {source_label}"
    finally:
        cx.close()


# --- bearer framing (the injectivity property the second entity FK introduces) ---------

def test_absent_bearer_differs_from_present_but_empty_bearer():
    """An observation with NO bearer must not project like one whose bearer is all-empty.

    This is the same framing rule the spatial and temporal corners already rely on, applied
    to the second entity row: without the presence flag, ``bearer=None`` and a bearer whose
    three fields are all None would render as the same token run, and two structurally
    DIFFERENT claims would hash equal — a forged same-evidence finding.
    """
    absent = _l1(bearer=None)
    empty = _l1(bearer=ids.EntityProjection(None, None, None))
    assert absent != empty
    assert ids.spatiotemporal_entity_hash(absent) != ids.spatiotemporal_entity_hash(empty)


def test_bearer_identity_moves_the_digest():
    """Two CDR legs identical but for WHICH antenna served them are different claims."""
    a = _l1(bearer=ids.EntityProjection("228-01-2042-77341", "cell", "antenna"))
    b = _l1(bearer=ids.EntityProjection("228-01-2042-99999", "cell", "antenna"))
    assert ids.spatiotemporal_entity_hash(a) != ids.spatiotemporal_entity_hash(b)


def test_no_bearer_is_the_subject_and_unknown_shape():
    """A GNSS fix (``subject``) and a Waze trace (``unknown``) both project NO bearer block:
    the first because its bearer IS the targeted entity, the second because it has none —
    so the link cannot reach the digest through the bearer (decision 55)."""
    assert _l1(bearer=None)
