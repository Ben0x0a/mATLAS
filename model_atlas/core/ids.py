"""Deterministic identity for the run database: uuid5 ids + the two evidence digests.

Defines:    one uuid5 namespace per run-DB table, the generic ``id_for(table, *parts)``
            helper and one explicit per-table derivation helper each (the id-scheme
            table of run_db_schema.md); the ``canonical_json`` determinism helper
            (rule 5); the plain typed projection-input dataclasses
            (``EntityProjection`` / ``SpatialProjection`` / ``TemporalProjection`` /
            ``ActivityProjection`` / ``ObservationLinks`` + their ``Component`` parts);
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
of its observations and asserts byte-equal digests, so any drift between this module and
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
# (run_db_schema.md §"id scheme": 'a | in a path forged equal keys'). 0x1F almost never
# occurs in a path, a CSV value, or a normalised identifier — but "almost never" is not
# "never": a pathological source value CAN carry a literal 0x1F, so ``_txt`` ESCAPES it
# (below) to guarantee it only ever appears as a genuine delimiter.
SEP = "\x1f"

# The escape introducer (ASCII 0x1B). WHY: to keep the join unforgeable, ``_txt`` must be
# able to represent a value that itself contains ``SEP`` (or this introducer) without that
# byte acting as a delimiter. Escaping ``_ESC``→``_ESC _ESC`` and ``SEP``→``_ESC SEP``
# makes the encoding unambiguous (a bare ``SEP`` is ALWAYS a delimiter, never data), so the
# token stream is uniquely recoverable and ``l1_projection`` stays injective.
_ESC = "\x1b"

# Optional-corner presence flags for the L1 projection. WHY two distinct flags: an absent
# corner and a present-but-empty corner (empty header, no components) must be
# distinguishable in the projection — otherwise ``_txt(None) == _txt("")`` would collapse
# them onto the same string. Each optional section starts with one of these at a fixed
# position, so "absent" (one flag slot, nothing after) can never render like "present with
# empty fields". These sit in dedicated frame slots a value never occupies, so their exact
# spelling is free; kept short because they only feed a sha256.
_CORNER_PRESENT = "+"
_CORNER_ABSENT = "-"

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
# DELIBERATELY FROZEN at its historical value: this is an opaque constant feeding uuid5,
# NOT the table name. Changing it would change every observation_id (and every analysis_id
# derived from one) and break cross-run diffability against every DB already built - the
# same argument made for _NAMESPACE above.
OBSERVATION_NAME = "assertion"
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
    """Render one id/digest part as a stable, SEP-safe token.

    HOW: an Enum becomes its ``.value`` (the bare stored token — ``str(member)`` would
    yield e.g. ``"ActivityRole.CALLER"`` on this Python, which the demo never stored), a
    ``None`` becomes the empty string, everything else its ``str()``; then the two reserved
    control bytes are escaped — ``_ESC``→``_ESC _ESC`` FIRST, then ``SEP``→``_ESC SEP`` —
    so the rendered token can never contain a bare ``SEP``.
    WHY empty-for-None: a missing part must be distinct-but-stable, never the literal
    "None" that could clash with a real value.
    WHY escape: without it a source value carrying a literal ``SEP`` (0x1F) would inject
    extra delimiter slots and let a structurally DIFFERENT claim hash equal — the join
    would not be injective (a forged same-claim/corroboration relationship). Escaping the
    introducer before the separator keeps decoding unambiguous, so the token stream is
    uniquely recoverable.
    """
    if part is None:
        return ""
    if isinstance(part, Enum):
        part = part.value
    # Escape the introducer FIRST, then SEP: order matters so ``_ESC SEP`` in the output
    # can only mean "escaped SEP", never "escaped _ESC followed by a real delimiter".
    return str(part).replace(_ESC, _ESC + _ESC).replace(SEP, _ESC + SEP)


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


def observation_id(raw_record_id_: str, preset_key: str, output_ordinal: int) -> str:
    """observation: uuid5(raw_record_id ⟂ preset KEY ⟂ output_ordinal) — the preset KEY
    (not preset_id) so the natural key is stable across a version bump;
    UNIQUE(raw_record_id, preset_id, output_ordinal)."""
    return id_for(OBSERVATION_NAME, raw_record_id_, preset_key, output_ordinal)


def entity_id(observation_natural_key: tuple[object, ...]) -> str:
    """entity: uuid5(observation natural key ⟂ role tag).

    The corner tag is the ``entity`` namespace itself (id_for prepends it), so the id is
    ``uuid5(entity ⟂ <observation natural key parts>)``. The observation natural key is
    passed as a tuple of its parts (raw_record_id, output_ordinal, and any further
    discriminator the assembler uses) — the assembler owns that shape; this helper only
    fixes the corner tag.

    An observation now owns TWO entity rows, so the caller MUST append a role part —
    ``"targeted"`` or ``"bearer"`` — as the last element of the natural key. Without it the
    two rows of one observation would derive the same uuid5 and collide on the primary key.
    Both remain per-observation OCCURRENCE rows: no content key, no sharing, no merge, and
    convergence stays ``GROUP BY (identifier_type, identifier)``."""
    return id_for(ENTITY_NAME, *observation_natural_key)


def spatial_expression_id(observation_natural_key: tuple[object, ...]) -> str:
    """spatial_expression: uuid5(observation natural key ⟂ corner tag 'sx')."""
    return id_for(SPATIAL_EXPRESSION_NAME, *observation_natural_key)


def temporal_expression_id(observation_natural_key: tuple[object, ...]) -> str:
    """temporal_expression: uuid5(observation natural key ⟂ corner tag 'tx')."""
    return id_for(TEMPORAL_EXPRESSION_NAME, *observation_natural_key)


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


def analysis_id(observation_id_: str, method: str) -> str:
    """analysis: uuid5(observation_id ⟂ method); UNIQUE(observation_id, method)."""
    return id_for(ANALYSIS_NAME, observation_id_, method)


def edit_log_id() -> str:
    """edit_log: a fresh uuid4 — an audit event has no natural key."""
    return str(uuid.uuid4())


# --- Canonical JSON (determinism rule 5) -----------------------------------

def canonical_json(obj: object) -> str:
    """Canonical JSON per run_db_schema.md §Determinism rule 5.

    HOW: ``json.dumps`` with sorted keys and the tightest separators (no insignificant
    whitespace), UTF-8-clean via ``ensure_ascii=False``, and ``allow_nan=False``.
    WHY allow_nan=False: the default (True) emits the non-standard ``NaN``/``Infinity``
    tokens — invalid JSON that no conformant parser can re-read, and EVERY NaN prints the
    same ``NaN`` token so distinct not-a-number contexts would collide in the digest.
    Refusing them makes an accidental float NaN/Inf raise loudly here rather than forge a
    silently-equal or unverifiable claim.
    WHY no float re-formatting: numeric SOURCE values are captured as STRINGS upstream
    (the assembler keeps the raw text — e.g. "46.2030"), so ``json.dumps`` only ever
    sees a str and can never turn "46.2030" into 46.203. Passing a float here would
    re-format it; the contract is that callers do not — this helper does not defend
    against it because the whole pipeline keeps values as source text (rule 5).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


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
    """One spatial component AS IT ENTERS THE DIGEST: part ⟂ unit ⟂ raw_value.

    ``unit`` replaces the former ``spatial_role`` slot. WHY it belongs in the digest: the
    unit is what makes a raw value mean something — "12.5" is a different speed in m/s and
    in km/h — so two presets declaring different units for one source value are stating
    DIFFERENT claims and must not converge on one ``claim_digest``.

    ``source_field`` is deliberately ABSENT — a renamed source column must not move a
    digest (normative exclusion). Components are hashed in ``ordinal`` order, so the
    caller supplies them already sorted (the tuple order IS the ordinal order).
    """

    part: str
    unit: str | None = None              # the preset-declared source unit, or None
    raw_value: str | None = None


@dataclass(frozen=True)
class TemporalComponentProjection:
    """One temporal component AS IT ENTERS THE DIGEST:
    part ⟂ stated_offset ⟂ raw_value.

    ``source_field`` is deliberately ABSENT (normative exclusion). ``stated_offset`` is
    the source's own per-bound offset text, verbatim.
    """

    part: str
    stated_offset: str | None = None
    raw_value: str | None = None


@dataclass(frozen=True)
class EntityProjection:
    """One entity row AS IT ENTERS THE DIGEST: identifier ⟂ identifier_type ⟂ type.

    Used for BOTH entity rows an observation owns — the targeted entity and the position
    bearer — since the digest reads the same three fields from each.

    (``activity_role`` is carried on ``ObservationLinks`` — the doc lists it inside the
    targeted-entity block, but it is an observation column, and it belongs to the targeted
    entity alone: a bearer has no part in the activity. The projection order is preserved by
    ``l1_projection`` regardless of which dataclass holds it.) ``concerns`` /
    ``concerns_uid`` / ``raw_identifier`` are deliberately ABSENT (normative exclusions).
    """

    identifier: str | None
    identifier_type: object            # IdentifierType enum or its str value
    type: object                       # EntityType enum or its str value


@dataclass(frozen=True)
class SpatialProjection:
    """The spatial expression AS IT ENTERS THE DIGEST: referential ⟂ spatial_source ⟂
    components (each part ⟂ unit ⟂ raw_value, in ordinal order)."""

    referential: object                # SpatialReferential enum or its str value
    spatial_source: str | None = None
    # mutable default — a shared [] across instances would be a latent aliasing bug.
    components: tuple[SpatialComponentProjection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TemporalProjection:
    """The temporal expression AS IT ENTERS THE DIGEST: kind ⟂ referential ⟂ components
    (each part ⟂ stated_offset ⟂ raw_value, in ordinal order)."""

    kind: object                       # TemporalKind enum or its str value
    referential: object | None = None  # TemporalReferential enum, its str value, or None
    components: tuple[TemporalComponentProjection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ObservationLinks:
    """The three corner links + attestation, AS THEY ENTER THE DIGEST.

    Projection order: ``activity_role`` sits with the targeted-entity block; the three links
    and ``attestation`` follow the temporal block. ``l1_projection`` places each correctly.

    ``recorded_at`` is deliberately ABSENT: it says when the RECORD was written, not what the
    record claims, so it is provenance — like paths and source attributes — and a re-export
    with a new write time must not move a claim's identity. ``attestation`` IS present: it is
    preset-mapped and part of what is being claimed.
    """

    activity_role: object | None = None   # ActivityRole enum or str value
    entity_link: object | None = None     # EntityLink enum or str value
    spatial_link: object | None = None    # SpatialLink enum or str value
    occupancy: object | None = None       # Occupancy enum or str value
    attestation: object | None = None     # Attestation enum or str value


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
    bearer: EntityProjection | None,
    spatial: SpatialProjection | None,
    temporal: TemporalProjection | None,
    links: ObservationLinks,
    deleted: object | None,
    multiplicity: int | None,
    activity: ActivityProjection | None,
) -> str:
    """The NORMATIVE L1 projection both digests hash (run_db_schema.md §"The normative
    L1 projection"), in the EXACT field order, joined with the unit separator.

    Order (verbatim from the doc):
      targeted entity (identifier ⟂ identifier_type ⟂ type ⟂ activity_role)
      ⟂ bearer (presence ⟂ [identifier ⟂ identifier_type ⟂ type])
      ⟂ spatial expression (presence ⟂ [referential ⟂ spatial_source ⟂ count ⟂
        components by ordinal, each part ⟂ unit ⟂ raw_value])
      ⟂ temporal expression (presence ⟂ [kind ⟂ referential ⟂ count ⟂ components by
        ordinal, each part ⟂ stated_offset ⟂ raw_value])
      ⟂ entity_link ⟂ spatial_link ⟂ occupancy ⟂ attestation ⟂ deleted ⟂ multiplicity
      ⟂ activity (presence ⟂ [kind ⟂ canonical context_json])

    WHY this framing makes the projection INJECTIVE (no two structurally-different claims
    share an L1 string). Two properties combine:
      1. ``_txt`` escapes ``SEP``, so a bare ``SEP`` is ALWAYS a delimiter — the token
         list is uniquely recoverable from the joined string, never smuggled by a value.
      2. Every variable-length section is FRAMED. The targeted-entity block is a fixed 4
         slots and the links+flags block a fixed 6, so their boundaries are positional. The
         BEARER is optional and therefore leads with a presence flag — at most one bearer
         exists, so a flag suffices and no component-style count is needed; without the flag
         an absent bearer would render identically to a present one whose three fields are
         all empty, and two structurally different claims would hash equal. Each
         optional corner leads with a presence flag (``_CORNER_PRESENT``/``_CORNER_ABSENT``)
         at a determined position; a PRESENT spatial/temporal corner then carries an
         explicit component COUNT before its rows, pinning exactly how many
         3-slot rows follow (spatial: part ⟂ unit ⟂ raw_value; temporal: part ⟂
         stated_offset ⟂ raw_value) — so the next corner's flag
         position is unambiguous. Hence the token list partitions back into a unique
         structured claim.
    Before this framing, an unframed component could be shifted across the spatial/
    temporal boundary (no count) and an absent corner rendered like a present-but-empty
    one (``_txt(None) == _txt("")``); both let a different claim hash equal.

    ABSENT vs PRESENT-BUT-EMPTY is now carried by the presence flag + count, NOT by
    ``_txt`` — an absent corner emits its flag alone; a present corner with an empty
    header still emits ``_CORNER_PRESENT`` and (for spatial/temporal) a ``0`` count.

    EXCLUDED normatively (never appear below): concerns, concerns_uid, both origin
    columns, raw_identifier, component source_fields, all paths, all *_id/run_id, all
    integrations, data_source, analysis. Their absence is the structural guard that a
    register/tzdb upgrade, an operator param, or a renamed column cannot move a digest.
    """
    parts: list[str] = []

    # targeted entity: identifier ⟂ identifier_type ⟂ type ⟂ activity_role (always 4 slots).
    parts += [
        _txt(entity.identifier),
        _txt(entity.identifier_type),
        _txt(entity.type),
        _txt(links.activity_role),
    ]

    # bearer: presence ⟂ [identifier ⟂ identifier_type ⟂ type]. Absent whenever the entity
    # holds its own position (entity_link is `self`), so the flag carries real information
    # and must be emitted even though the block is only three slots wide.
    if bearer is None:
        parts += [_CORNER_ABSENT]
    else:
        parts += [
            _CORNER_PRESENT,
            _txt(bearer.identifier),
            _txt(bearer.identifier_type),
            _txt(bearer.type),
        ]

    # spatial expression: presence ⟂ [referential ⟂ spatial_source ⟂ count ⟂ rows].
    if spatial is None:
        parts += [_CORNER_ABSENT]
    else:
        # The count frames the component list so it cannot bleed into the temporal corner.
        parts += [
            _CORNER_PRESENT,
            _txt(spatial.referential),
            _txt(spatial.spatial_source),
            str(len(spatial.components)),
        ]
        for c in spatial.components:   # supplied already in ordinal order
            parts += [_txt(c.part), _txt(c.unit), _txt(c.raw_value)]

    # temporal expression: presence ⟂ [kind ⟂ referential ⟂ count ⟂ rows].
    if temporal is None:
        parts += [_CORNER_ABSENT]
    else:
        parts += [
            _CORNER_PRESENT,
            _txt(temporal.kind),
            _txt(temporal.referential),
            str(len(temporal.components)),
        ]
        for c in temporal.components:
            parts += [
                _txt(c.part),
                _txt(c.stated_offset),
                _txt(c.raw_value),
            ]

    # the three corner links ⟂ attestation ⟂ deleted ⟂ multiplicity (always 6 slots).
    parts += [
        _txt(links.entity_link),
        _txt(links.spatial_link),
        _txt(links.occupancy),
        _txt(links.attestation),
        _txt(deleted),
        _txt(multiplicity),
    ]

    # activity: presence ⟂ [kind ⟂ canonical context_json] (re-canonicalised so key order
    # can't drift). No component list, so the presence flag alone frames it.
    if activity is None:
        parts += [_CORNER_ABSENT]
    else:
        ctx = activity.context
        parts += [
            _CORNER_PRESENT,
            _txt(activity.kind),
            "" if ctx is None else canonical_json(ctx),
        ]

    return SEP.join(parts)


# --- The two evidence digests ----------------------------------------------

def _sha256_hex(text: str) -> str:
    """sha256 of the UTF-8 bytes, as 64-char lowercase hex — the stored digest form."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim_digest(l1: str) -> str:
    """``observation.claim_digest`` = sha256 of the L1 projection ALONE.

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
    """``observation.content_hash`` (CSV ``content_uid``) =
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
    """Convenience: (content_hash, claim_digest) for one observation from its L1 projection.

    WHY paired: the assembler always needs both for one observation row and they share the
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
