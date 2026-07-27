"""Deterministic identity for the run database: uuid5 ids + the two evidence digests.

Defines:    one uuid5 namespace per run-DB table, the generic ``id_for(table, *parts)``
            helper and one explicit per-table derivation helper each (the id-scheme
            table of run_db_schema.md); the ``canonical_json`` determinism helper
            (rule 5); the plain typed projection-input dataclasses
            (``EntityProjection`` / ``SpatialProjection`` / ``TemporalProjection`` /
            ``ActivityProjection`` / ``AssertionLinks`` + their ``Component`` parts);
            the normative ``l1_projection`` both digests hash; and the two digests
            ``content_hash`` and ``claim_digest``.
Used by:    the assembly brick (it builds the projection inputs and calls
            ``l1_projection`` / ``content_hash`` / ``claim_digest`` with no store), the
            store's id derivation, and the demo cross-check tests.
Depends on: the standard library only (``dataclasses``, ``enum``, ``hashlib``, ``json``,
            ``uuid``) and — for the enum→token rendering only — ``enum.Enum``.

WHY a whole module for identity: the ids and digests ARE the identity discipline
(identity_model.md, run_db_schema.md "id scheme"). Getting a join separator or a
projected field wrong silently forges or splits evidence — so every rule the two docs
state is expressed here once, in code, with the doc's field order preserved verbatim.

Cross-check: docs/examples/build_example_db.py transcribed the SAME normative recipes
by hand into docs/examples/matlas_example.sqlite; tests/core/test_ids.py rebuilds three
of its assertions and asserts byte-equal digests, so any drift between this module and
the doc surfaces as a red test rather than a silent divergence.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum


# --- The uuid5 namespace + the join separator ------------------------------
# HOW: one fixed root namespace, then the table tag is the FIRST joined part, so two
# logically distinct rows that happen to share natural-key text can never collide (a
# ``source`` keyed on a path and an ``archive`` keyed on the same path derive different
# ids because "source" / "archive" lead their joins). This mirrors the demo's single
# ``NS`` + table-tag-first scheme exactly, so the ids reproduce byte-for-byte.
# WHY this exact UUID: it is the value the worked-example demo committed; changing it would break
# cross-run diffability against every DB already built with the demo recipe.
_NAMESPACE = uuid.UUID("9c4e7b1a-2d63-5f48-8a17-3b5c6d0e9f21")

# The unit separator (ASCII 0x1F). WHY NOT "|": a "|" can occur inside a real path or
# value, so joining on it lets a crafted path forge a different split that hashes equal
# (run_db_schema.md §"id scheme": 'a | in a path forged equal keys'). 0x1F cannot occur
# in a path, a CSV value, or a normalised identifier, so the split is unforgeable.
SEP = "\x1f"

# Per-table namespace tags (the FIRST join part). Suffixed _NAME because each is a
# string identifier, not a value (house rule). One tag per run-DB table; the id-scheme
# table names every one. The demo used short tags (record/sx/tx/sc/tc/si/ti/s2/actrec)
# for four tables — those exact spellings are reproduced in the per-table helpers below
# so the demo cross-check stays byte-equal; here we keep the full table names as the
# canonical public vocabulary and map the demo's short forms where they differ.
CASE_METADATA_NAME = "case_metadata"
RUN_NAME = "run"
ARCHIVE_NAME = "archive"
RUN_ARCHIVE_NAME = "run_archive"
SOURCE_NAME = "source"
PRESET_NAME = "preset"
RUN_PRESET_NAME = "run_preset"
RAW_RECORD_NAME = "record"            # demo tag: "record" (not "raw_record")
ACTIVITY_NAME = "activity"
ACTIVITY_RECORD_NAME = "actrec"       # demo tag: "actrec"
ASSERTION_NAME = "assertion"
ENTITY_NAME = "entity"
SPATIAL_EXPRESSION_NAME = "sx"        # demo tag: "sx"
TEMPORAL_EXPRESSION_NAME = "tx"       # demo tag: "tx"
SPATIAL_COMPONENT_NAME = "sc"         # demo tag: "sc"
TEMPORAL_COMPONENT_NAME = "tc"        # demo tag: "tc"
SPATIAL_INTEGRATION_NAME = "si"       # demo tag: "si"
TIMESPAN_INTEGRATION_NAME = "ti"      # demo tag: "ti"
SPATIAL_INDEX_NAME = "s2"             # demo tag: "s2"
ANALYSIS_NAME = "analysis"
EDIT_LOG_NAME = "edit_log"


def _txt(part: object) -> str:
    """Render one id/digest part as a stable token.

    HOW: an Enum becomes its ``.value`` (the bare stored token — ``str(member)`` would
    yield e.g. ``"EntityRole.CALLER"`` on this Python, which the demo never stored), a
    ``None`` becomes the empty string, everything else its ``str()``.
    WHY empty-for-None: a missing part must be distinct-but-stable, never the literal
    "None" that could clash with a real value; and a NULL corner must hash identically
    whether the caller passes ``None`` or a field left unset.
    """
    if part is None:
        return ""
    if isinstance(part, Enum):
        return str(part.value)
    return str(part)


def id_for(table: str, *parts: object) -> str:
    """uuid5 over (table-tag ⟂ natural-key parts), joined with the unit separator.

    HOW: the table tag leads the join (so cross-table text collisions are impossible),
    each remaining part is rendered by ``_txt`` and joined with ``SEP``.
    WHY deterministic: no wall-clock, no counter — the SAME natural key always yields the
    SAME id, so two runs over the same evidence are diffable (run_id feeds no child id).
    """
    joined = SEP.join(_txt(p) for p in (table, *parts))
    return str(uuid.uuid5(_NAMESPACE, joined))


# --- Per-table derivation helpers (the id-scheme table) --------------------
# Each helper names its inputs explicitly so a caller cannot silently pass the wrong
# natural key; the id-scheme table of run_db_schema.md is the authority for every one.
# The two uuid4 roots (case, run) and the audit-event uuid4 (edit_log) are the only
# non-deterministic ids — a fresh random id per occurrence, feeding NO child id, so two
# runs over the same evidence stay diffable.


def case_metadata_id() -> str:
    """case_metadata: a fresh uuid4 (the DB root — one per DB)."""
    return str(uuid.uuid4())


def run_id() -> str:
    """run: a fresh uuid4 — the only non-deterministic evidence root; feeds no child id,
    so two runs over the same evidence produce equal downstream ids."""
    return str(uuid.uuid4())


def archive_id(path: str) -> str:
    """archive: uuid5(path) — from the path ONLY (§2c: a content hash is
    never a key), so 'same bytes twice' stays a GROUP BY cd_sha256 finding, not a merge."""
    return id_for(ARCHIVE_NAME, path)


def run_archive_id(run_id_: str, archive_id_: str) -> str:
    """run_archive: uuid5(run_id ⟂ archive_id) — a run-scoped link, so the diffability
    rule exempts it; UNIQUE(run_id, archive_id)."""
    return id_for(RUN_ARCHIVE_NAME, run_id_, archive_id_)


def run_preset_id(run_id_: str, preset_id_: str) -> str:
    """run_preset: uuid5(run_id ⟂ preset_id) — run-scoped link;
    UNIQUE(run_id, preset_id)."""
    return id_for(RUN_PRESET_NAME, run_id_, preset_id_)


def source_id(input_file_path: str, inner_path: str | None, subunit: str | None) -> str:
    """source: uuid5(input_file_path ⟂ inner_path ⟂ subunit) — from PATHS, NEVER the
    fingerprint (§2c fix), and preset-free. So two byte-identical exhibits
    stay two source rows whose matching fingerprint is a finding, not a collapse."""
    return id_for(SOURCE_NAME, input_file_path, inner_path, subunit)


def preset_id(key: str, version: str | None) -> str:
    """preset: uuid5(key ⟂ version) — the ONLY content-keyed table: a preset's
    definition is its identity; UNIQUE(key, version)."""
    return id_for(PRESET_NAME, key, version)


def raw_record_id(source_id_: str, line_no: int) -> str:
    """raw_record: uuid5(source_id ⟂ line_no); UNIQUE(source_id, line_no)."""
    return id_for(RAW_RECORD_NAME, source_id_, line_no)


def activity_id(raw_record_id_: str, activity_ordinal: int) -> str:
    """activity: uuid5(raw_record_id ⟂ activity_ordinal) — per-record at ingest."""
    return id_for(ACTIVITY_NAME, raw_record_id_, activity_ordinal)


def activity_record_id(activity_id_: str, raw_record_id_: str) -> str:
    """activity_record: uuid5(activity_id ⟂ raw_record_id); UNIQUE(activity_id,
    raw_record_id) — ingest writes exactly one per activity."""
    return id_for(ACTIVITY_RECORD_NAME, activity_id_, raw_record_id_)


def assertion_id(raw_record_id_: str, preset_key: str, output_ordinal: int) -> str:
    """assertion: uuid5(raw_record_id ⟂ preset KEY ⟂ output_ordinal) — the preset KEY
    (not preset_id) so the natural key is stable across a version bump;
    UNIQUE(raw_record_id, preset_id, output_ordinal)."""
    return id_for(ASSERTION_NAME, raw_record_id_, preset_key, output_ordinal)


def entity_id(assertion_natural_key: tuple[object, ...]) -> str:
    """entity: uuid5(assertion natural key ⟂ corner tag).

    The corner tag is the ``entity`` namespace itself (id_for prepends it), so the id is
    ``uuid5(entity ⟂ <assertion natural key parts>)``. The assertion natural key is
    passed as a tuple of its parts (raw_record_id, output_ordinal, and any further
    per-corner discriminator the assembler uses) — the assembler owns that shape; this
    helper only fixes the corner tag."""
    return id_for(ENTITY_NAME, *assertion_natural_key)


def spatial_expression_id(assertion_natural_key: tuple[object, ...]) -> str:
    """spatial_expression: uuid5(assertion natural key ⟂ corner tag 'sx')."""
    return id_for(SPATIAL_EXPRESSION_NAME, *assertion_natural_key)


def temporal_expression_id(assertion_natural_key: tuple[object, ...]) -> str:
    """temporal_expression: uuid5(assertion natural key ⟂ corner tag 'tx')."""
    return id_for(TEMPORAL_EXPRESSION_NAME, *assertion_natural_key)


def spatial_component_id(spatial_expression_id_: str, ordinal: int) -> str:
    """spatial_component: uuid5(spatial_expression_id ⟂ ordinal); UNIQUE(expr, ordinal)."""
    return id_for(SPATIAL_COMPONENT_NAME, spatial_expression_id_, ordinal)


def temporal_component_id(temporal_expression_id_: str, ordinal: int) -> str:
    """temporal_component: uuid5(temporal_expression_id ⟂ ordinal); UNIQUE(expr, ordinal)."""
    return id_for(TEMPORAL_COMPONENT_NAME, temporal_expression_id_, ordinal)


def spatial_integration_id(spatial_expression_id_: str, method: str) -> str:
    """spatial_integration: uuid5(spatial_expression_id ⟂ method)."""
    return id_for(SPATIAL_INTEGRATION_NAME, spatial_expression_id_, method)


def timespan_integration_id(temporal_expression_id_: str, method: str) -> str:
    """timespan_integration: uuid5(temporal_expression_id ⟂ method)."""
    return id_for(TIMESPAN_INTEGRATION_NAME, temporal_expression_id_, method)


def spatial_index_id(spatial_integration_id_: str) -> str:
    """spatial_index: uuid5(spatial_integration_id) — 1:1 with the integration."""
    return id_for(SPATIAL_INDEX_NAME, spatial_integration_id_)


def analysis_id(assertion_id_: str, method: str) -> str:
    """analysis: uuid5(assertion_id ⟂ method); UNIQUE(assertion_id, method)."""
    return id_for(ANALYSIS_NAME, assertion_id_, method)


def edit_log_id() -> str:
    """edit_log: a fresh uuid4 — an audit event has no natural key."""
    return str(uuid.uuid4())


# --- Canonical JSON (determinism rule 5) -----------------------------------

def canonical_json(obj: object) -> str:
    """Canonical JSON per run_db_schema.md §Determinism rule 5.

    HOW: ``json.dumps`` with sorted keys and the tightest separators (no insignificant
    whitespace), UTF-8-clean via ``ensure_ascii=False``.
    WHY no float re-formatting: numeric SOURCE values are captured as STRINGS upstream
    (the assembler keeps the raw text — e.g. "46.2030"), so ``json.dumps`` only ever
    sees a str and can never turn "46.2030" into 46.203. Passing a float here would
    re-format it; the contract is that callers do not — this helper does not defend
    against it because the whole pipeline keeps values as source text (rule 5).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_order_json(obj: object) -> str:
    """Like ``canonical_json`` but PRESERVING key order (the source's own column order).

    Used for ``raw_record.raw_json`` so the exported ``orig_<col>`` passthrough follows the
    source's column order rather than a sorted one (better readability / recognisability).
    Safe to differ from canonical here: ``raw_json`` feeds NO digest — the content/claim
    digests hash the L1 projection, never the raw row — so key order carries no determinism
    consequence for evidence identity. Same tight separators / ``ensure_ascii`` as
    ``canonical_json``; only the key ORDER differs (Python dicts preserve insertion order).
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# --- The typed projection input (what the assembler builds; no store) ------
# Plain frozen dataclasses so the assemble brick can construct the exact projection
# input without touching a store or the wider family dataclasses. Fields are ONLY the
# ones the L1 projection reads — deliberately narrow, so a reader sees at a glance that
# excluded columns (concerns, raw_identifier, source_field, ids, integrations) cannot
# reach a digest.


@dataclass(frozen=True)
class SpatialComponentProjection:
    """One spatial component AS IT ENTERS THE DIGEST: part ⟂ spatial_role ⟂ raw_value.

    ``source_field`` is deliberately ABSENT — a renamed source column must not move a
    digest (normative exclusion). Components are hashed in ``ordinal`` order, so the
    caller supplies them already sorted (the tuple order IS the ordinal order).
    """

    part: str
    spatial_role: object | None = None   # SpatialRole enum or its str value, or None
    raw_value: str | None = None


@dataclass(frozen=True)
class TemporalComponentProjection:
    """One temporal component AS IT ENTERS THE DIGEST:
    part ⟂ temporal_role ⟂ stated_offset ⟂ raw_value.

    ``source_field`` is deliberately ABSENT (normative exclusion). ``stated_offset`` is
    the source's own per-bound offset text, verbatim.
    """

    part: str
    temporal_role: object | None = None   # TemporalRole enum or its str value, or None
    stated_offset: str | None = None
    raw_value: str | None = None


@dataclass(frozen=True)
class EntityProjection:
    """The entity corner AS IT ENTERS THE DIGEST: identifier ⟂ identifier_type ⟂ type.

    (``entity_role`` is carried on ``AssertionLinks`` — the doc lists it inside the
    entity block, but it is an assertion column; the projection order is preserved by
    ``l1_projection`` regardless of which dataclass holds it.) ``concerns`` /
    ``concerns_uid`` / ``raw_identifier`` are deliberately ABSENT (normative exclusions).
    """

    identifier: str | None
    identifier_type: object            # IdentifierType enum or its str value
    type: object                       # EntityType enum or its str value


@dataclass(frozen=True)
class SpatialProjection:
    """The spatial expression AS IT ENTERS THE DIGEST: referential ⟂ spatial_source ⟂
    components (each part ⟂ spatial_role ⟂ raw_value, in ordinal order)."""

    referential: object                # SpatialReferential enum or its str value
    spatial_source: str | None = None
    # mutable default — a shared [] across instances would be a latent aliasing bug.
    components: tuple[SpatialComponentProjection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TemporalProjection:
    """The temporal expression AS IT ENTERS THE DIGEST: kind ⟂ referential ⟂ components
    (each part ⟂ temporal_role ⟂ stated_offset ⟂ raw_value, in ordinal order)."""

    kind: object                       # TemporalKind enum or its str value
    referential: object | None = None  # TemporalReferential enum, its str value, or None
    components: tuple[TemporalComponentProjection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AssertionLinks:
    """The four-way triptych columns on the assertion, AS THEY ENTER THE DIGEST.

    The doc's projection order is: entity_role (with the entity block), then the three
    remaining links after the temporal block. ``l1_projection`` places each correctly.
    """

    entity_role: object | None = None            # EntityRole enum or str value
    entity_position_link: object | None = None   # EntityPositionLink enum or str value
    entity_time_link: object | None = None       # EntityTimeLink enum or str value
    spatial_temporal_link: object | None = None  # SpatialTemporalLink enum or str value


@dataclass(frozen=True)
class ActivityProjection:
    """The activity AS IT ENTERS THE DIGEST: kind ⟂ canonical context_json.

    ``context`` is the DECODED preset-mapped context object (a dict or None), NOT a
    pre-serialised string: ``l1_projection`` re-canonicalises it so key order in the
    source can never move the digest (a party silently vanishing WOULD move it — that is
    a real change and must move the digest). This mirrors the demo, which hands the raw
    dict to the projection.
    """

    kind: object                       # ActivityKind enum or its str value
    context: object | None = None      # decoded context object (dict) or None


# --- The normative L1 projection (run_db_schema.md) ------------------------

def l1_projection(
    *,
    entity: EntityProjection,
    spatial: SpatialProjection | None,
    temporal: TemporalProjection | None,
    links: AssertionLinks,
    deleted: object | None,
    multiplicity: int | None,
    activity: ActivityProjection | None,
) -> str:
    """The NORMATIVE L1 projection both digests hash (run_db_schema.md §"The normative
    L1 projection"), in the EXACT field order, joined with the unit separator.

    Order (verbatim from the doc):
      entity (identifier ⟂ identifier_type ⟂ type ⟂ entity_role)
      ⟂ spatial expression (referential ⟂ spatial_source ⟂ components by ordinal, each
        part ⟂ spatial_role ⟂ raw_value)
      ⟂ temporal expression (kind ⟂ referential ⟂ components by ordinal, each
        part ⟂ temporal_role ⟂ stated_offset ⟂ raw_value)
      ⟂ the three links ⟂ deleted ⟂ multiplicity
      ⟂ activity (kind ⟂ canonical context_json)

    A NULL corner contributes exactly two empty slots (its two header fields, no
    component rows) — WHY: an assertion with no place and one whose place header happens
    to be empty must be indistinguishable here only if both truly carry nothing, and the
    two-empty-slot shape is what the demo hashed, so the cross-check pins it.

    EXCLUDED normatively (never appear below): concerns, concerns_uid, both origin
    columns, raw_identifier, component source_fields, all paths, all *_id/run_id, all
    integrations, data_source, analysis. Their absence is the structural guard that a
    register/tzdb upgrade, an operator param, or a renamed column cannot move a digest.
    """
    parts: list[str] = []

    # entity corner: identifier ⟂ identifier_type ⟂ type ⟂ entity_role
    parts += [
        _txt(entity.identifier),
        _txt(entity.identifier_type),
        _txt(entity.type),
        _txt(links.entity_role),
    ]

    # spatial expression: referential ⟂ spatial_source ⟂ components(part, role, raw)
    if spatial is None:
        parts += ["", ""]   # the two header slots for a missing corner
    else:
        parts += [_txt(spatial.referential), _txt(spatial.spatial_source)]
        for c in spatial.components:   # supplied already in ordinal order
            parts += [_txt(c.part), _txt(c.spatial_role), _txt(c.raw_value)]

    # temporal expression: kind ⟂ referential ⟂ components(part, role, stated_offset, raw)
    if temporal is None:
        parts += ["", ""]
    else:
        parts += [_txt(temporal.kind), _txt(temporal.referential)]
        for c in temporal.components:
            parts += [
                _txt(c.part),
                _txt(c.temporal_role),
                _txt(c.stated_offset),
                _txt(c.raw_value),
            ]

    # the three links ⟂ deleted ⟂ multiplicity
    parts += [
        _txt(links.entity_position_link),
        _txt(links.entity_time_link),
        _txt(links.spatial_temporal_link),
        _txt(deleted),
        _txt(multiplicity),
    ]

    # activity: kind ⟂ canonical context_json (re-canonicalised so key order can't drift)
    if activity is None:
        parts += ["", ""]
    else:
        ctx = activity.context
        parts += [_txt(activity.kind), "" if ctx is None else canonical_json(ctx)]

    return SEP.join(parts)


# --- The two evidence digests ----------------------------------------------

def _sha256_hex(text: str) -> str:
    """sha256 of the UTF-8 bytes, as 64-char lowercase hex — the stored digest form."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim_digest(l1: str) -> str:
    """``assertion.claim_digest`` = sha256 of the L1 projection ALONE.

    No fingerprint, no line_no, no output_ordinal, no preset — so two overlapping
    disclosures (different files sharing the same record content) CONVERGE here and
    surface as a *possible same-evidence* finding, never presented as corroboration.
    Repeats legally; never a key.
    """
    return _sha256_hex(l1)


def content_hash(
    *,
    fingerprint_sha256: str | None,
    preset_key: str,
    line_no: int,
    output_ordinal: int,
    l1: str,
) -> str:
    """``assertion.content_hash`` (CSV ``content_uid``) =
    sha256(fingerprint_sha256 ⟂ preset_key ⟂ line_no ⟂ output_ordinal ⟂ L1 projection).

    The digest SCOPED to this preset's reading of this file (the preset key is in the
    prefix). NO paths, so folder==zip parity holds; the fingerprint distinguishes
    two byte-DIFFERENT files carrying the same claim (they share ``claim_digest`` but not
    ``content_hash``). Repeats legally; never a key.
    """
    prefix = SEP.join(
        [_txt(fingerprint_sha256), preset_key, str(line_no), str(output_ordinal), l1]
    )
    return _sha256_hex(prefix)


def digests(
    *,
    fingerprint_sha256: str | None,
    preset_key: str,
    line_no: int,
    output_ordinal: int,
    l1: str,
) -> tuple[str, str]:
    """Convenience: (content_hash, claim_digest) for one assertion from its L1 projection.

    WHY paired: the assembler always needs both for one assertion row and they share the
    L1 string, so computing them together avoids re-projecting.
    """
    return (
        content_hash(
            fingerprint_sha256=fingerprint_sha256,
            preset_key=preset_key,
            line_no=line_no,
            output_ordinal=output_ordinal,
            l1=l1,
        ),
        claim_digest(l1),
    )
