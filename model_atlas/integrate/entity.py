"""Entity normalisation: fills the entity row's derived fields, per kind, with
refuse semantics — then recomputes the evidence digests the new identifier moves.

Defines:    ``normalise_identifier`` (one raw identifier -> its canonical form or None,
            the REFUSE verdict, per the docs/entity_model.md rule table),
            ``stem_concerns`` / ``concerns_uid`` (the case-folded/stemmed
            attribution digest: "A. Moreau" / "moreau a." / "A.MOREAU" share one uid),
            ``normalise_entities`` (the store-level driver: one narrow UPDATE per entity
            row setting ONLY identifier / identifier_type / concerns_uid, plus the
            observation-digest recomputation described below and the rule-set stamps in
            ``matlas_meta``), and ``EntityNormaliseReport`` (its testable summary).
Used by:    the app-layer pipeline (wires L2 after ingest) and
            tests/integrate/test_entity.py.
Depends on: ``model_atlas.core`` (``ids`` — the normative L1 projection + both digests,
            ``store`` — the run DB connection) and the standard library only
            (``dataclasses``, ``hashlib``, ``ipaddress``, ``json``, ``logging``, ``re``,
            ``sqlite3``, ``unicodedata``).

Refuse semantics (docs/entity_model.md — the design's whole point)
---------------------------------------------------------------------
A normaliser may REFUSE rather than guess: a refusal leaves ``identifier`` NULL, so the
mention can never converge with anything — convergence is always the query
``GROUP BY identifier_type, identifier`` over NON-NULL identifiers, never a stored
merge, and this brick writes per-row derived values only (no dedup, no row sharing).

Refusal deliberately does NOT rewrite ``identifier_type``: the stored kind is the
preset's CLAIM ("this string was declared an msisdn"), it sits INSIDE the hashed L1
projection, and both observation digests already committed to it at assembly.
Erasing it to ``none`` would either move the evidence digests for a value that was
merely *declined* (a refusal is a non-derivation, not new evidence) or leave the stored
digests stale against a fresh recomputation — both unacceptable. Only ``identifier``
legitimately moves the digests (``concerns_uid`` is normatively excluded),
so a refused row's observation digests are byte-identical before and after this brick;
the NULL identifier alone guarantees nothing converges. This is a deliberate deviation
from the original design note's shorthand "identifier_type=none on refusal", resolved in
favour of digest integrity.

The digest recomputation (CRITICAL — run_db_schema.md, the normative L1 projection)
-----------------------------------------------------------------------------------
``entity.identifier`` is inside the L1 projection both ``observation_content_hash`` and
``spatiotemporal_entity_hash`` hash. Assembly stored digests hashing ``identifier=None`` (the assembled,
pre-normalisation row), so after a normalisation SUCCEEDS this brick re-reads every L1
row the projection covers (entity, spatial/temporal expressions + components in ordinal
order), rebuilds the projection via ``ids.l1_projection`` and both
digests via ``ids.digests`` — never a local recipe — and UPDATEs the two observation
columns. Cost shape: O(1) small SELECTs per normalised entity (the observation is 1:1
with its entity by the schema's UNIQUE), so the whole pass is linear in the number of
successfully normalised rows.

Why the direct UPDATEs are not an evidence mutation (the store has no update API)
---------------------------------------------------------------------------------
The RunStore exposes insert-only writers by design; this brick uses narrow SQL UPDATEs
through ``store.connection`` because it completes a DEFERRED DERIVATION, not an edit:
``identifier`` / ``identifier_type`` / ``concerns_uid`` are the derived columns of the
lean entity row (this collapsed the old L2 entity integration onto the row) and
the two digest columns are derived summaries of the L1 projection. The evidence itself
— ``raw_identifier``, ``concerns``, every component ``raw_value`` — is never touched,
and the applied rule-set versions are recorded once per run in ``matlas_meta``
(*derived value => documented derivation*).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from model_atlas.core import ids
from model_atlas.core.store import RunStore

_LOG = logging.getLogger(__name__)

# The rule-set versions this module implements, recorded in matlas_meta by
# ``normalise_entities`` (the normalisation + stemming rule-set
# versions are recorded once per run). The region calling-code table below is PART of
# entity_norm/1.0 — extending it is a rule-set change and must bump this version.
NORMALISATION_RULESET = "entity_norm/1.0"
STEMMING_RULESET = "concerns_stem/1.0"

# ISO region -> E.164 country calling code, for msisdn national forms (trunk-0) and as
# the plate country qualifier fallback. Deliberately SMALL and explicit: an unknown
# region REFUSES rather than guessing a country code (a wrong code would silently
# converge two different subscribers — the exact failure refuse semantics exist for).
_REGION_CALLING_CODE: dict[str, str] = {
    "CH": "41", "FR": "33", "DE": "49", "IT": "39", "AT": "43", "LI": "423",
    "GB": "44", "ES": "34", "PT": "351", "BE": "32", "NL": "31", "LU": "352",
    "US": "1", "CA": "1",
}

# Visual separators stripped from phone/IMEI/IMSI text before validation. WHY listed
# once: the same "spacing is presentation, digits are identity" rule serves all three
# digit-string kinds, so one pattern keeps them consistent.
_DIGIT_SEPARATORS_RE = re.compile(r"[\s().\-/]")

# The <qualifier>:<value> shape shared by account / camera / case_ref: a leading
# alphanumeric qualifier token, one colon, a non-blank remainder.
_QUALIFIED_RE = re.compile(r"^([A-Za-z][\w.\-]*):(\S.*)$")

# An explicit country prefix on a plate ("CH:VD 12345"): 2-3 letters + colon.
_PLATE_COUNTRY_RE = re.compile(r"^([A-Za-z]{2,3}):(.+)$")


# --- per-kind normalisers (docs/entity_model.md rule table) -----------------
# Uniform shape: (raw text, preset default region) -> canonical string, or None = the
# REFUSE verdict. Every function is pure so the rule table is testable in isolation.

def _normalise_msisdn(raw: str, region: str | None) -> str | None:
    """msisdn -> E.164 (``+<digits>``); requires country context, else refuse.

    HOW: strip visual separators, then read the international form (leading ``+`` or
    ``00`` prefix) directly, or the national trunk-0 form through the preset default
    region's calling code; finally require 6-15 pure digits (the E.164 length window).
    WHY refuse a bare national number without a region: prefixing a guessed country
    code would let two different subscribers converge — the region is the only honest
    source of that context. A number with neither ``+``/``00`` nor a trunk 0 is
    ambiguous (already-international? national?) and refuses for the same reason.
    """
    cleaned = _DIGIT_SEPARATORS_RE.sub("", raw.strip())
    if cleaned.startswith("+"):
        digits = cleaned[1:]
    elif cleaned.startswith("00"):
        digits = cleaned[2:]
    elif cleaned.startswith("0"):
        calling_code = _REGION_CALLING_CODE.get((region or "").upper())
        if calling_code is None:
            return None
        digits = calling_code + cleaned[1:]
    else:
        return None
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def _normalise_imei(raw: str, region: str | None) -> str | None:
    """imei -> the 14-digit body (check digit dropped — entity_model.md rule table).

    A 15-digit IMEI drops its Luhn check digit; a 16-digit IMEISV drops its two
    software-version digits; a 14-digit body passes through. Anything else refuses —
    padding or truncating would forge a different handset.
    """
    digits = _DIGIT_SEPARATORS_RE.sub("", raw.strip())
    if not digits.isdigit() or len(digits) not in (14, 15, 16):
        return None
    return digits[:14]


def _normalise_imsi(raw: str, region: str | None) -> str | None:
    """imsi -> global canonical digit string (separators stripped, 6-15 digits)."""
    digits = _DIGIT_SEPARATORS_RE.sub("", raw.strip())
    if not digits.isdigit() or not 6 <= len(digits) <= 15:
        return None
    return digits


def _normalise_email(raw: str, region: str | None) -> str | None:
    """email -> lowercase; refuse anything without exactly one @ and a dotted domain.

    WHY the dotted-domain requirement: a bare host ("x@localhost") is not a globally
    convergent address, and guessing a domain suffix would forge equality.
    """
    text = raw.strip()
    if text.count("@") != 1 or any(character.isspace() for character in text):
        return None
    local, _, domain = text.partition("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return text.lower()


def _normalise_plate(raw: str, region: str | None) -> str | None:
    """plate -> ``<COUNTRY>:<BODY>`` — country-qualified, spacing/dash stripped.

    The country comes from an explicit ``CC:`` prefix in the value, else from the
    preset default region; with neither, refuse (an unqualified plate could collide
    across countries — "GE 1234" exists in several registers).
    """
    text = raw.strip().upper()
    prefixed = _PLATE_COUNTRY_RE.match(text)
    if prefixed is not None:
        country, body = prefixed.group(1), prefixed.group(2)
    elif region:
        country, body = region.upper(), text
    else:
        return None
    body = re.sub(r"[\s.\-]", "", body)
    if not body or not body.isalnum():
        return None
    return f"{country}:{body}"


def _normalise_account(raw: str, region: str | None) -> str | None:
    """account -> ``<service>:<handle>`` — canonical ONLY with a service qualifier.

    A bare handle refuses: "@x" on Telegram and "@x" on X are two accounts, so
    without the service there is nothing global to converge on. The service token is
    case-folded; the handle is kept verbatim (handle case-sensitivity is the
    service's business, not ours to guess).
    """
    match = _QUALIFIED_RE.match(raw.strip())
    if match is None:
        return None
    return f"{match.group(1).lower()}:{match.group(2)}"


def _normalise_ip(raw: str, region: str | None) -> str | None:
    """ip -> compressed canonical text, PUBLIC addresses only.

    ``is_global`` (IANA special-purpose registries) excludes private ranges, CGNAT
    100.64/10, loopback, link-local and reserved space — equal private IPs on two
    networks are not the same endpoint, so they refuse; multicast is excluded too
    (a group address never identifies one endpoint).
    """
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if not address.is_global or address.is_multicast:
        return None
    return str(address)


def _normalise_mac(raw: str, region: str | None) -> str | None:
    """mac -> lowercase colon-separated; universally-administered unicast only.

    The locally-administered bit (0x02 of the first octet) covers modern randomised
    MACs — two randomised addresses matching means nothing, so they refuse; the
    multicast bit (0x01) refuses because a group address identifies no device.
    """
    cleaned = re.sub(r"[\s:.\-]", "", raw.strip()).lower()
    if re.fullmatch(r"[0-9a-f]{12}", cleaned) is None:
        return None
    first_octet = int(cleaned[:2], 16)
    if first_octet & 0b10 or first_octet & 0b01:
        return None
    return ":".join(cleaned[index:index + 2] for index in range(0, 12, 2))


def _normalise_cell(raw: str, region: str | None) -> str | None:
    """cell -> ``mcc-mnc-lac-ci`` within the cell-register namespace.

    The four digit groups are kept VERBATIM (an MNC "01" is not "1" — leading zeros
    are significant in the register); anything but exactly four digit groups refuses.
    """
    groups = re.split(r"[\s:\-/]+", raw.strip())
    if len(groups) != 4 or not all(group.isdigit() for group in groups):
        return None
    return "-".join(groups)


def _normalise_camera(raw: str, region: str | None) -> str | None:
    """camera -> ``<register>:<asset id>`` within its register namespace; a bare
    asset id refuses (asset "42" exists in every register). The register token is
    case-folded; the asset id stays verbatim."""
    match = _QUALIFIED_RE.match(raw.strip())
    if match is None:
        return None
    return f"{match.group(1).lower()}:{match.group(2)}"


def _normalise_case_ref(raw: str, region: str | None) -> str | None:
    """case_ref -> ``<issuer>:<ref>`` within the issuing system; a bare reference
    refuses (case "2026/123" exists in every registry). Issuer case-folded, the
    reference verbatim."""
    match = _QUALIFIED_RE.match(raw.strip())
    if match is None:
        return None
    return f"{match.group(1).lower()}:{match.group(2)}"


# kind -> normaliser. A kind ABSENT here is never normalised: ``none`` (a purely typed
# mention — 'the device', a person name, a label) and ``other`` (a kind with no
# declared rule) are skipped entirely, not refused — refusal means a rule EXAMINED a
# value and declined; here no rule exists to apply. This is how "device / person /
# label / unknown are never normalised" is expressed in the lean row: such mentions
# carry identifier_type='none', so no rule ever touches them (§2d stays closed).
_NORMALISERS = {
    "msisdn": _normalise_msisdn,
    "imei": _normalise_imei,
    "imsi": _normalise_imsi,
    "email": _normalise_email,
    "plate": _normalise_plate,
    "account": _normalise_account,
    "ip": _normalise_ip,
    "mac": _normalise_mac,
    "cell": _normalise_cell,
    "camera": _normalise_camera,
    "case_ref": _normalise_case_ref,
}


def normalise_identifier(kind: str, raw: str, *, default_region: str | None = None) -> str | None:
    """The canonical identifier for one raw value under ``kind``'s rule, or None.

    None is the REFUSE verdict (or an un-normalisable kind): the mention keeps a NULL
    identifier and can never converge. Pure function — the driver below applies it
    row by row and the tests exercise the rule table through it directly.
    """
    normaliser = _NORMALISERS.get(kind)
    if normaliser is None:
        return None
    return normaliser(raw, default_region)


# --- concerns stemming + digest -----------------------------------------------

def stem_concerns(text: str) -> str:
    """The stemmed grouping form of a ``concerns`` value (rule-set concerns_stem/1.0).

    HOW: Unicode NFC (so composed/decomposed accents cannot split a group), casefold,
    every non-word run (dots, commas, hyphens, apostrophes, underscores, spaces)
    collapsed to one space, tokens SORTED and re-joined — so "A. Moreau" /
    "moreau a." / "A.MOREAU" all stem to "a moreau".
    WHY token sorting rather than heavier stemming: name order is presentation
    ("Moreau A." vs "A. Moreau"), but diacritics are kept ("Müller" is not "Muller")
    — over-merging would fabricate an attribution the evidence never made.
    """
    folded = unicodedata.normalize("NFC", text).casefold()
    cleaned = re.sub(r"[\W_]+", " ", folded)
    return " ".join(sorted(cleaned.split()))


def concerns_uid(text: str) -> str:
    """``entity.concerns_uid``: sha256 hex of the stemmed concerns text.

    Derived, NEVER a key, and normatively excluded from both evidence digests —
    it only powers the attribution GROUP BY; the rows stay distinct.
    """
    return hashlib.sha256(stem_concerns(text).encode("utf-8")).hexdigest()


# --- the store-level driver ---------------------------------------------------

@dataclass
class EntityNormaliseReport:
    """The counts one ``normalise_entities`` call produced (testable summary)."""

    normalised_count: int = 0          # rows whose identifier was filled
    refused_count: int = 0             # rows a rule examined and declined
    concerns_count: int = 0            # rows given a concerns_uid
    digests_recomputed_count: int = 0  # observations whose two digests were rewritten


def normalise_entities(store: RunStore, *, default_region: str | None = None) -> EntityNormaliseReport:
    """Fill every entity row's derived fields and recompute the digests they move.

    HOW: one deterministic pass over ``entity`` (ordered by id): apply the kind's rule
    to ``raw_identifier`` (success fills ``identifier``; refusal leaves it NULL — see
    the module header for why the claimed ``identifier_type`` is preserved either
    way), derive ``concerns_uid`` from ``concerns``, write ONE narrow UPDATE per row
    touching only the three derived columns, and — whenever the identifier was filled
    — recompute both digests of the (unique) observation referencing the row. Finally
    stamp the applied rule-set versions into ``matlas_meta``.
    Convergence stays a QUERY (GROUP BY identifier_type, identifier): this function
    never merges, dedups or deletes a row.
    """
    connection = store.connection
    report = EntityNormaliseReport()
    rows = connection.execute(
        "SELECT entity_id, raw_identifier, identifier_type, type, concerns "
        "FROM entity ORDER BY entity_id").fetchall()
    for entity_id, raw_identifier, identifier_type, entity_type, concerns in rows:
        identifier: str | None = None
        # A rule only runs when the kind declares one AND the source wrote a value —
        # a claimed kind with no text is nothing examined, so it is skipped, not refused.
        if identifier_type in _NORMALISERS and raw_identifier is not None:
            identifier = normalise_identifier(
                identifier_type, raw_identifier, default_region=default_region)
            if identifier is None:
                report.refused_count += 1
            else:
                report.normalised_count += 1
        uid = concerns_uid(concerns) if concerns is not None else None
        if uid is not None:
            report.concerns_count += 1
        if identifier is None and uid is None:
            continue   # nothing derived — the row stays exactly as assembled
        # The ONE narrow UPDATE: only the derived columns (module header — completing a
        # deferred derivation, not editing evidence). identifier_type is restated at its
        # stored value: rule-set 1.0 never rewrites the claimed kind, because the kind is
        # inside the hashed claim and only ``identifier`` may legitimately move a digest.
        connection.execute(
            "UPDATE entity SET identifier = ?, identifier_type = ?, concerns_uid = ? "
            "WHERE entity_id = ?",
            (identifier, identifier_type, uid, entity_id))
        if identifier is not None:
            report.digests_recomputed_count += _recompute_observation_digests(
                connection, entity_id, identifier, identifier_type, entity_type)
    # Record the rule-set versions actually applied: the schema wrote
    # placeholders at creation; the derivation is only documented once these are real.
    connection.execute(
        "UPDATE matlas_meta SET normalisation_ruleset = ?, stemming_ruleset = ?",
        (NORMALISATION_RULESET, STEMMING_RULESET))
    _LOG.info(
        f"entity normalisation ({NORMALISATION_RULESET}, {STEMMING_RULESET}): "
        f"{report.normalised_count} normalised, {report.refused_count} refused, "
        f"{report.concerns_count} concerns uids, "
        f"{report.digests_recomputed_count} observation digest pairs recomputed")
    return report


# --- the digest recomputation (never a local recipe) --------------------------

def _recompute_observation_digests(
    connection: sqlite3.Connection,
    entity_id: str,
    identifier: str,
    identifier_type: str,
    entity_type: str,
) -> int:
    """Recompute + UPDATE both digests of the observation referencing ``entity_id``.

    HOW: read the observation (1:1 with its entity — both entity FKs are UNIQUE in the
    schema), re-read every L1 row the normative projection covers, rebuild the projection
    with the NEW identifier through ``ids.l1_projection`` and both digests through
    ``ids.digests`` — the same normative recipes the assembler used, so the stored
    digests always equal a fresh recomputation from the rows. Returns the number of
    observations updated (0 when the entity has none — legal on a partial store).
    WHY faithful re-reading matters: a locally re-derived projection that dropped one
    corner or reordered one component would silently forge or split evidence; every
    piece below mirrors ``ingest.assemble._emit_observation`` exactly.

    WHY the lookup matches EITHER FK: an observation owns two entity rows, and normalising
    the BEARER (an antenna's cell ref, a camera's asset id) moves the digest exactly as
    normalising the targeted entity does — both sit in the L1 projection. Matching only
    ``entity_id`` would find nothing for a bearer row and silently skip its digest update,
    leaving a stored digest that no longer equals a fresh recomputation. That is precisely
    the drift this function exists to prevent.
    """
    observation = connection.execute(
        "SELECT observation_id, raw_record_id, preset_id, "
        "spatial_expression_id, temporal_expression_id, "
        "entity_id, bearer_entity_id "
        "FROM observation WHERE entity_id = ? OR bearer_entity_id = ?",
        (entity_id, entity_id)).fetchone()
    if observation is None:
        return 0
    (observation_id, raw_record_id, preset_id,
     spatial_expression_id, temporal_expression_id,
     targeted_entity_id, bearer_entity_id) = observation

    # Both entity rows enter the projection, so BOTH are read from the store — with the
    # freshly normalised values substituted for whichever row triggered this call.
    def _entity_projection(row_id: str | None) -> ids.EntityProjection | None:
        if row_id is None:
            return None
        if row_id == entity_id:
            return ids.EntityProjection(identifier, identifier_type, entity_type)
        stored = connection.execute(
            "SELECT identifier, identifier_type, type FROM entity WHERE entity_id = ?",
            (row_id,)).fetchone()
        return None if stored is None else ids.EntityProjection(*stored)

    # The observation_content_hash prefix inputs: fingerprint ⟂ preset KEY ⟂ line_no.
    line_no, fingerprint_sha256 = connection.execute(
        "SELECT record.line_no, source_file.fingerprint_sha256 FROM raw_record AS record "
        "JOIN source_file ON source_file.source_file_id = record.source_file_id "
        "WHERE record.raw_record_id = ?",
        (raw_record_id,)).fetchone()
    (preset_key,) = connection.execute(
        "SELECT key FROM preset WHERE preset_id = ?", (preset_id,)).fetchone()

    targeted_projection = _entity_projection(targeted_entity_id)
    assert targeted_projection is not None, (
        f"observation {observation_id} has no targeted entity row")
    # A `subject` row's bearer IS the targeted entity: the projection then carries no bearer
    # block, exactly as assemble wrote it (ids.l1_projection, decision 55).
    bearer_projection = (None if bearer_entity_id == targeted_entity_id
                         else _entity_projection(bearer_entity_id))
    l1 = ids.l1_projection(
        entity=targeted_projection,
        bearer=bearer_projection,
        spatial=_load_spatial_projection(connection, spatial_expression_id),
        temporal=_load_temporal_projection(connection, temporal_expression_id),
    )
    observation_content_hash, spatiotemporal_entity_hash = ids.digests(
        fingerprint_sha256=fingerprint_sha256,
        preset_key=preset_key,
        line_no=line_no,
        l1=l1,
    )
    connection.execute(
        "UPDATE observation SET observation_content_hash = ?, spatiotemporal_entity_hash = ? WHERE observation_id = ?",
        (observation_content_hash, spatiotemporal_entity_hash, observation_id))
    return 1


def _load_spatial_projection(
    connection: sqlite3.Connection, spatial_expression_id: str | None,
) -> ids.SpatialProjection | None:
    """The stored spatial corner as its digest projection (components in ordinal
    order — the tuple order IS the ordinal order the digest hashes), or None when the
    observation carries no spatial corner (the projection's two-empty-slot shape)."""
    if spatial_expression_id is None:
        return None
    referential, spatial_source = connection.execute(
        "SELECT referential, spatial_source FROM spatial_expression "
        "WHERE spatial_expression_id = ?", (spatial_expression_id,)).fetchone()
    components = tuple(
        ids.SpatialComponentProjection(part, unit, raw_value)
        for part, unit, raw_value in connection.execute(
            "SELECT part, unit, raw_value FROM spatial_component "
            "WHERE spatial_expression_id = ? ORDER BY ordinal",
            (spatial_expression_id,)))
    return ids.SpatialProjection(referential, spatial_source, components)


def _load_temporal_projection(
    connection: sqlite3.Connection, temporal_expression_id: str | None,
) -> ids.TemporalProjection | None:
    """The stored temporal corner as its digest projection (bounds in ordinal order,
    each with its own stated_offset), or None when the corner is absent."""
    if temporal_expression_id is None:
        return None
    kind, referential = connection.execute(
        "SELECT kind, referential FROM temporal_expression "
        "WHERE temporal_expression_id = ?", (temporal_expression_id,)).fetchone()
    components = tuple(
        ids.TemporalComponentProjection(part, stated_offset, raw_value)
        for part, stated_offset, raw_value in connection.execute(
            "SELECT part, stated_offset, raw_value FROM temporal_component "
            "WHERE temporal_expression_id = ? ORDER BY ordinal",
            (temporal_expression_id,)))
    return ids.TemporalProjection(kind, referential, components)
