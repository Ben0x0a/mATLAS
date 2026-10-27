"""Deterministic identity for the run database: uuid5 ids + the two evidence digests.

Defines:    one uuid5 namespace per run-DB table, the generic ``id_for(table, *parts)``
            helper and one explicit per-table derivation helper each (the id-scheme
            table of run_db_schema.md); the ``canonical_json`` determinism helper
            (rule 5); the plain typed projection-input dataclasses
            (``EntityProjection`` / ``SpatialProjection`` / ``TemporalProjection`` /
            ``ActivityProjection`` + their ``Component`` parts);
            the normative ``l1_projection`` both digests hash; and the two digests
            ``observation_content_hash`` and ``spatiotemporal_entity_hash``.
Used by:    the assembly brick (it builds the projection inputs and calls
            ``l1_projection`` / ``observation_content_hash`` / ``spatiotemporal_entity_hash`` with no store), the
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
PRESET_NAME = "preset"
RUN_PRESET_NAME = "run_preset"
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


def new_id() -> str:
    """A fresh uuid4 for one EVIDENCE-CHAIN row (decision 50).

    ``source_file``, ``raw_record``, ``observation``, ``entity``, the two expressions and
    their components, the two integrations, ``spatial_index`` and ``analysis`` all take one
    of these. **Full random**: no derivation, no seed, no timestamp, no node id.

    WHY random rather than derived
    ------------------------------
    An id's only job is to DISTINGUISH rows, not to order them and not to detect duplicates.
    Duplicate evidence is found by ``spatiotemporal_entity_hash``, which is content-derived
    and therefore converges ACROSS files — the thing a uuid5 over a path-derived natural key
    could never do, since a different file gave a different id by construction.

    WHY not uuid6/uuid7: both are TIME-ORDERED and embed the processing machine's clock (and
    a node/clock-sequence). Putting the examiner's wall clock inside an evidence row id is
    the opposite of what an id is for.

    WHY it is never seeded: a seeded stream is POSITION-dependent — id N depends on how many
    were drawn before it, so appending a source shifts every later id — and seeding from
    something like the case name makes two same-named cases collide outright. Test
    reproducibility comes from injecting a factory, never from making evidence ids
    predictable.

    Ordering: ingest order is not an order that should interest an analyst. The temporal
    columns are the only ordering the model offers.
    """
    return str(uuid.uuid4())


def preset_id(key: str, version: str | None) -> str:
    """preset: uuid5(key ⟂ version) — the ONLY content-keyed table: a preset's
    definition is its identity; UNIQUE(key, version).

    Kept DERIVED while the evidence chain went random (decision 50): a preset id is
    meaningful ACROSS cases — the same key+version is the same preset everywhere — which is
    exactly the property an evidence-row id must not have."""
    return id_for(PRESET_NAME, key, version)


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
    DIFFERENT claims and must not converge on one ``spatiotemporal_entity_hash``.

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

``concerns`` / ``concerns_uid`` / ``raw_identifier`` are
    deliberately ABSENT (normative exclusions).
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


def l1_projection(
    *,
    entity: EntityProjection,
    bearer: EntityProjection | None,
    spatial: SpatialProjection | None,
    temporal: TemporalProjection | None,
) -> str:
    """The NORMATIVE L1 projection both digests hash (run_db_schema.md §"The normative
    L1 projection"), in the EXACT field order, joined with the unit separator.

    Order (verbatim from the doc):
      targeted entity (identifier ⟂ identifier_type ⟂ type)
      ⟂ bearer (presence ⟂ [identifier ⟂ identifier_type ⟂ type])
      ⟂ spatial expression (presence ⟂ [referential ⟂ spatial_source ⟂ count ⟂
        components by ordinal, each part ⟂ unit ⟂ raw_value])
      ⟂ temporal expression (presence ⟂ [kind ⟂ referential ⟂ count ⟂ components by
        ordinal, each part ⟂ stated_offset ⟂ raw_value])

    NO LINKS (decision 55). A link is the preset author's statement about a record, not
    content of the record: two presets reading the same record must converge on the same
    ``spatiotemporal_entity_hash`` even when they disagree on a link — the disagreement stays
    visible in the link columns. ``observation_content_hash`` still separates presets
    through its ``preset_key`` prefix.

    ``bearer`` is None when no OTHER entity holds the position — including ``subject``, where
    the bearer is the targeted entity itself. Projecting it there would repeat the entity
    block and make the hash depend on the link after all (``subject`` vs ``unknown``).

    WHY this framing makes the projection INJECTIVE (no two structurally-different claims
    share an L1 string). Two properties combine:
      1. ``_txt`` escapes ``SEP``, so a bare ``SEP`` is ALWAYS a delimiter — the token
         list is uniquely recoverable from the joined string, never smuggled by a value.
      2. Every variable-length section is FRAMED. The targeted-entity block is a fixed 3
         slots, so its boundary is positional. The
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

    # targeted entity: identifier ⟂ identifier_type ⟂ type (always 3 slots).
    parts += [
        _txt(entity.identifier),
        _txt(entity.identifier_type),
        _txt(entity.type),
    ]

    # bearer: presence ⟂ [identifier ⟂ identifier_type ⟂ type]. Absent whenever no OTHER
    # entity holds the position (subject, unknown), so the flag carries real information
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

    # ``recovery_state`` / ``recovery_source`` are deliberately EXCLUDED, like paths and
    # source attributes: HOW a record was obtained is provenance, not identity. A claim
    # carved from free space is the SAME claim as a live one, only less certain to have
    # been truly recorded — so the two must CONVERGE for the duplicate-surfacing to work
    # across a live and a recovered copy of one row.
    return SEP.join(parts)


# --- The two evidence digests ----------------------------------------------

def _sha256_hex(text: str) -> str:
    """sha256 of the UTF-8 bytes, as 64-char lowercase hex — the stored digest form."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spatiotemporal_entity_hash(l1: str) -> str:
    """``observation.spatiotemporal_entity_hash`` = sha256 of the L1 projection ALONE.

    No fingerprint, no line_no, no preset — so two overlapping
    disclosures (different files sharing the same record content) CONVERGE here and
    surface as a *possible same-evidence* finding, never presented as corroboration.
    Repeats legally; never a key.
    """
    return _sha256_hex(l1)


def observation_content_hash(
    *,
    fingerprint_sha256: str | None,
    preset_key: str,
    line_no: int,
    l1: str,
) -> str:
    """``observation.observation_content_hash`` (CSV ``observation_content_uid``) =
    sha256(fingerprint_sha256 ⟂ preset_key ⟂ line_no ⟂ L1 projection).

    ``output_ordinal`` left the prefix with the column (decision 50). Two templates of one
    record now share a hash ONLY when their L1 projections are byte-identical — i.e. only
    when they really are the same claim, which is more correct than separating them by a
    position index.

    The digest SCOPED to this preset's reading of this file (the preset key is in the
    prefix). NO paths, so folder==zip parity holds; the fingerprint distinguishes
    two byte-DIFFERENT files carrying the same claim (they share ``spatiotemporal_entity_hash`` but not
    ``observation_content_hash``). Repeats legally; never a key.
    """
    prefix = SEP.join([_txt(fingerprint_sha256), preset_key, str(line_no), l1])
    return _sha256_hex(prefix)


def digests(
    *,
    fingerprint_sha256: str | None,
    preset_key: str,
    line_no: int,
    l1: str,
) -> tuple[str, str]:
    """Convenience: (observation_content_hash, spatiotemporal_entity_hash) for one observation from its L1 projection.

    WHY paired: the assembler always needs both for one observation row and they share the
    L1 string, so computing them together avoids re-projecting.
    """
    return (
        observation_content_hash(
            fingerprint_sha256=fingerprint_sha256,
            preset_key=preset_key,
            line_no=line_no,
            l1=l1,
        ),
        spatiotemporal_entity_hash(l1),
    )
