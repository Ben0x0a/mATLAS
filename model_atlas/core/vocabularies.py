"""The single vocabulary + family declaration for the 21-table run database.

Defines:    every controlled-vocabulary enum (the three corner links + ``Attestation``,
            ``ActivityRole``, the corner referentials/kinds, the
            identifier/entity/activity/edit vocabularies), the component ``part``
            vocabularies + ``is_standard_part`` predicate, the phrase fragments used by
            the verbalizer, one frozen dataclass per table, and the ``column_cast`` /
            ``CANONICAL_UNIT`` / ``unit_factor`` helpers describing the integration
            columns.
Used by:    the schema builder (DDL + CHECKs derive one-directionally from these
            enums), the assembly/ingest engine (casts + unit conversion), the flat
            CSV projection, and the verbalizer (reads the ``phrase`` fragments).
Depends on: the standard library only (``dataclasses``, ``enum``).

This is the successor to ``model_atlas/model/families.py``. It is authoritative:
the run_db_schema.md TABLE definitions and its decision log (1-41) win over the
doc's older "Proposed families.py" code block wherever they disagree — so roles and
``stated_offset`` live on the COMPONENTS, ``granularity`` is dropped, and the
``archive`` / ``run_archive`` / ``run_preset`` / ``definition_digest`` /
``edit_log.ordinal`` additions are present here.

Derivation discipline (why so many enums but no free strings on typed columns): an
invalid vocabulary value is caught the moment a preset is parsed or the DDL CHECK is
built, never written silently into the run DB.

ONE AXIS PER FIELD. The former ``EntityPositionLink`` carried four independent axes at once
— geometric coupling, mechanism, stance and epistemics — so every write discarded three of
them, and the derived views ``Genesis`` / ``Locates`` existed only to project one axis back
out of that conflation, lossily (``at`` and ``claimed_at`` both rendered ``self``). Both
views are deleted here along with the enums that needed them. What replaces them is one
stored link per corner, each answering exactly one question, plus ``Attestation`` at
statement level: the one axis that is genuinely orthogonal to all three and therefore could
never be folded in (the 2026-07-11 decision that tried).

The two derivations that remain (``EntityLink.needs_bearer``, ``SpatialLink.is_presence``)
are the honest kind: each reads a SINGLE orthogonal field, so neither can lose information
the way the deleted views did.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# --- The three corner links + the statement-level attestation ---------------
# ONE stored link per corner, each answering ONE question and deliberately NOT answering the
# other two — the boundaries are what stop the axes re-conflating. The former
# ``EntityPositionLink`` mixed geometric coupling, mechanism, stance and epistemics into a
# single enum, so every write discarded an axis; ``Genesis`` / ``Locates`` then existed only
# to project one axis back out of that conflation, and did so lossily (``at`` and
# ``claimed_at`` both rendered ``self``). Both derived views are gone with the enum that
# needed them.
#
# Each phrased member declares a British-English ``phrase`` fragment beside its value: the
# verbalizer assembles one deterministic sentence from these fragments, so a member's wording
# is declared here once and derived nowhere else.


class EntityLink(str, Enum):
    """Entity corner — *through what interaction do we know this entity's position?*

    The relation between the observation's TARGETED entity and its position BEARER (the
    entity whose position the spatial corner actually states). ``SELF`` means the two are the
    same, so ``observation.bearer_entity_id`` is NULL; every other member requires a bearer.

    Does NOT answer: where (the spatial corner's content), how closely the geometry binds
    (``SpatialLink``), when (the temporal corner), or who asserts it (``Attestation``).

    That a member reads as both *what happened* (``used_at``, ``stayed_at``) and *how we
    know* is not a conflation: in forensic data the interaction IS the epistemic route — the
    trace exists because the interaction happened. It is also why the verb alone decides
    whether there is a spatial claim at all: ``CACHED`` leaves none.

    SELECTION RULE: use the verb the source ATTESTS, never one inferred. If a source cannot
    distinguish ``served_by`` from ``cached``, the answer is ``UNKNOWN`` — never a guess.

    EXTENSIBILITY: reviewed-extensible, not frozen. The 2026-08-30 removal of
    spatial_role/temporal_role is the precedent (two shipped presets needed values the CHECK
    rejected). The verb differs from those roles — it is attested by the record's nature
    rather than asserted by the preset author — but additions must still be reviewed, because
    the spatial claim depends on this value.
    """

    SELF = "self", "was at"                        # the entity's own stated position
    SERVED_BY = "served_by", "was served by"       # network infrastructure served it
    SEEN_BY = "seen_by", "was seen by"             # a sensor OR an observer registered it
    USED_AT = "used_at", "was used at"             # ATM, POS terminal, badge reader
    STAYED_AT = "stayed_at", "stayed at"           # hotel, hospital, custody register
    SEARCHED_FOR = "searched_for", "searched for"  # the entity's OWN interest in a place
    SHARED = "shared", "shared"                    # a place sent to or from the entity
    CACHED = "cached", "cached"                    # stored as data; no attested contact
    UNKNOWN = "unknown", "was linked to"           # interaction not determinable

    def __new__(cls, value: str, phrase: str) -> "EntityLink":
        # HOW: a str-Enum member is its own string value; ``phrase`` is attached as an extra
        # attribute so ``member.value`` stays the bare stored token.
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def needs_bearer(self) -> bool:
        """True when the observation MUST carry a ``bearer_entity_id``.

        The bearer invariant in ONE place: a bearer exists iff the link is not ``SELF``, so
        the column and the link can never contradict. Unlike the deleted ``Genesis`` /
        ``Locates``, this derives from an ORTHOGONAL field rather than a conflated one, so it
        loses nothing on the way.
        """
        return self is not EntityLink.SELF


class SpatialLink(str, Enum):
    """Spatial corner — *how closely does the geometry bind the entity?*

    Polarity and tightness of the targeted entity's relation to the geometry.

    Does NOT answer: the geometry's SHAPE (point / disc / wedge / region — a property of the
    spatial expression), the MEASUREMENT ERROR
    (``spatial_integration.horizontal_accuracy_m``), or whether the relation held for the
    whole interval (``Occupancy``).

    NOT derivable from ``EntityLink``: the same verb yields different relations depending on
    the geometry's granularity — ``self`` + a GNSS fix is ``AT`` a point, ``self`` + an IP
    geolocation is ``WITHIN`` a city polygon; ``used_at`` an ATM is ``AT``, ``used_at`` a
    card record naming only "SHELL FRANCE" is ``WITHIN``. Hence stored, never computed.

    THE THREE NEGATIVES ARE NOT INTERCHANGEABLE, and separating them is the point:
    ``REFERENCES`` = no presence claim but a deliberate connection (the entity was interested
    in the place); ``ABSENT`` = a positive claim of NON-presence (an alibi); ``UNKNOWN`` = no
    claim either way, which must be SURFACED for review, never silently dropped.

    Maps onto RCC-8 for query-time reasoning: AT ~ equal/inside, WITHIN ~ proper part,
    NEAR ~ externally connected, ABSENT ~ disconnected.
    """

    # The fragments are BARE RELATIONAL phrases, carrying no verb of their own: the sentence
    # supplies "was" when the entity holds its own position, and the entity-link clause
    # supplies it otherwise ("was served by <bearer>, near <place>"). A phrase that embedded
    # its own verb would read as "was served by X, was near Y".
    AT = "at", "at"                             # the geometry's extent IS the claim's extent
    WITHIN = "within", "somewhere within"       # inside a region larger than the entity
    NEAR = "near", "near"                       # close to, not necessarily inside
    REFERENCES = "references", "referring to"   # pointed at, not occupied
    ABSENT = "absent", "not at"                 # asserted NOT there (alibi / exclusion)
    UNKNOWN = "unknown", "in an undetermined relation to"

    def __new__(cls, value: str, phrase: str) -> "SpatialLink":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def is_presence(self) -> bool:
        """True when this link asserts the entity was actually there.

        The ONE presence predicate. It replaces the concomitance engine's three-token test
        (``entity_position_link = 'references'`` OR ``never_during_interval`` on EITHER the
        entity-time or the spatial-temporal edge) — negation used to be storable on two enums
        at once with no rule for disagreement. ``UNKNOWN`` is deliberately NOT presence and
        NOT an exclusion either: unknown is not non-presence, and dropping unmapped rows
        would hide evidence.
        """
        return self in (SpatialLink.AT, SpatialLink.WITHIN, SpatialLink.NEAR)


class Occupancy(str, Enum):
    """Temporal corner — *how much of the interval does the claim cover?*

    A modal quantifier over the stated interval, and the successor to
    ``SpatialTemporalLink`` — which described the ENTITY's occupancy while being labelled an
    edge between the space and time corners. ``CONTINUOUS`` is box (always); ``ONCE`` and
    ``SPORADIC`` are diamond (eventually).

    Does NOT answer: when the interval is (the temporal corner's content), how precisely the
    bounds are known (``timespan_integration.*_accuracy_us``), or whether the relation holds
    at all (``SpatialLink``).

    NO ``instant`` member: an instant is the DEGENERATE interval ``lower == upper``, where
    box and diamond coincide, so the field would carry no information. NO ``never`` member:
    negation is polarity and lives on ``SpatialLink.ABSENT``. Keeping negation on ONE corner
    is forced rather than stylistic — modal duality gives NOT-diamond-P == box-NOT-P, so
    allowing it on both would let one fact be written two ways, which is exactly the bug
    ``NEVER_DURING_INTERVAL`` caused by existing on two enums at once.

    ``ONCE`` and ``SPORADIC`` are both diamond but differ in the cardinality of witnesses:
    exactly one versus one-or-more. UNRELATED to ``observation.multiplicity``, which counts
    aggregate SOURCE RECORDS ("137 calls in March"), not occurrences of presence.
    """

    CONTINUOUS = "continuous", "continuously during"  # box — holds throughout
    ONCE = "once", "once during"                      # diamond — exactly once, point unknown
    SPORADIC = "sporadic", "sporadically during"      # diamond — at least once, repeated
    UNKNOWN = "unknown", "at an undetermined point during"

    def __new__(cls, value: str, phrase: str) -> "Occupancy":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


class Attestation(str, Enum):
    """Statement level — *who asserts this?* — never a corner, and never a weight.

    What KIND of party put this claim into the record. A fact observable in the source, not
    a judgement: evidential weight, credibility and admissibility are COURT questions and the
    model must not state them. The intelligence/admiralty grading (source reliability x
    information credibility) is deliberately NOT adopted for that reason; if ever wanted it
    belongs to an analyst layer, attributed to whoever graded it, never to ingest.

    It qualifies the WHOLE statement rather than one argument of it — the correct structure
    for a reified ternary relation: one qualifier per argument, plus qualifiers on the
    statement. Hence a plain ``observation`` column, not a fourth corner.

    Orthogonal to ``EntityLink``, which is why the 2026-07-11 decision to fold the epistemic
    qualifier INTO the link vocabularies could never work: ``served_by`` from an operator CDR
    is a ``SYSTEM_RECORD``; the same ``served_by`` asserted by a witness is a ``PERSON``.
    One verb, two attestors.

    ``THIRD_PARTY_REGISTER`` names a provenance the model previously had no vocabulary for:
    a location-cache coordinate comes from neither the source nor our engine but from a
    commercial database of unknown vintage. ``source.source_tier`` describes OUR processing,
    not that.
    """

    # The fragments are SELF-CONTAINED noun phrases, not transitive verbs needing an object:
    # attestation names the KIND of attestor, and no column carries the attestor's identity,
    # so "as recorded by a system" renders while a bare "recorded by" would dangle.
    INSTRUMENT = "instrument", "as measured by an instrument"        # GNSS, ANPR camera
    SYSTEM_RECORD = "system_record", "as recorded by a system"       # CDR, bank, hotel PMS
    PERSON = "person", "as stated by a person"                       # witness, alibi
    THIRD_PARTY_REGISTER = "third_party_register", "as supplied by a third-party register"
    UNKNOWN = "unknown", "as attested by an unknown party"

    def __new__(cls, value: str, phrase: str) -> "Attestation":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


class ActivityRole(str, Enum):
    """The targeted entity's part in the ACTIVITY — not its part in the placement.

    The successor to ``EntityRole``, minus the three members that were never activity parts:
    ``subject`` is now the plain meaning of ``observation.entity_id`` (the targeted entity),
    ``reporter`` is ``Attestation.PERSON``, and ``owner`` is an inter-entity relation
    belonging to the deferred analyst graph (entity_model.md), not an observation column.
    """

    CALLER = "caller", "the caller"
    CALLEE = "callee", "the callee"
    SENDER = "sender", "the sender"
    RECIPIENT = "recipient", "the recipient"
    UNKNOWN = "unknown", "an entity"

    def __new__(cls, value: str, phrase: str) -> "ActivityRole":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


# --- Corner referentials, kinds and their roles ---------------------------

class IdentifierType(str, Enum):
    """The identifier kinds the normaliser knows how to canonicalise.

    These are the KNOWN/suggested kinds, NOT a closed constraint: ``entity.identifier_type``
    is a free string column, so a preset may name any kind it likes; an unrecognised one is
    simply never normalised (``identifier`` stays NULL). ``NONE`` = a purely typed mention
    ('the device'): ``identifier`` stays NULL, so nothing can ever converge on it.
    """

    MSISDN = "msisdn"
    IMEI = "imei"
    IMSI = "imsi"
    EMAIL = "email"
    PLATE = "plate"
    ACCOUNT = "account"
    MAC = "mac"
    IP = "ip"
    CELL = "cell"
    CAMERA = "camera"
    CASE_REF = "case_ref"
    OTHER = "other"
    NONE = "none"


class EntityType(str, Enum):
    """What the thing IS — never an identifier (the §2d fix).

    Decision 36 adds ``EVENT`` (an event from a structured list IS the entity) and
    ``POI`` (a placed point of interest). Consolidates the ⊕ camera addition.
    """

    DEVICE = "device"
    PERSON = "person"
    VEHICLE = "vehicle"
    ACCOUNT = "account"
    CAMERA = "camera"
    ANTENNA = "antenna"
    CASE = "case"
    LABEL = "label"
    EVENT = "event"    # a placed event from a structured list
    POI = "poi"        # a placed point of interest
    UNKNOWN = "unknown"


class SpatialReferential(str, Enum):
    """Encoding / reference system of the raw place value (NOT its role).

    Consolidates the ⊕ asset_ref addition. Extend with further EPSG codes as sources
    require them — a single-place change.
    """

    EPSG_4326 = "epsg:4326"
    GEOHASH = "geohash"
    MGRS = "mgrs"
    ADDRESS = "address"
    PLACE_NAME = "place_name"
    CELL_REF = "cell_ref"
    ASSET_REF = "asset_ref"
    REGION = "region"
    UNKNOWN = "unknown"




class TemporalKind(str, Enum):
    """Shape of the temporal expression.

    Carries a ``phrase`` fragment: the sentence differs for an instant vs an
    interval, so the verbaliser reads the kind to choose 'at <instant>' or
    'from <lower> to <upper>'.
    """

    # An INSTANT is the degenerate interval ``lower == upper``, so this member is a
    # statement about the EXPRESSION's shape (what the source wrote), never about occupancy —
    # ``Occupancy`` deliberately has no ``instant``, because box and diamond coincide there.
    INSTANT = "instant", "at"
    INTERVAL = "interval", "from"
    OPEN_LOWER = "open_lower", "until"
    OPEN_UPPER = "open_upper", "from"

    def __new__(cls, value: str, phrase: str) -> "TemporalKind":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


class TemporalReferential(str, Enum):
    """Format / reference system of the raw time value."""

    UNIX_EPOCH_S = "unix_epoch_s"
    UNIX_EPOCH_MS = "unix_epoch_ms"
    UNIX_EPOCH_US = "unix_epoch_us"
    APPLE_CFABSOLUTE = "apple_cfabsolute"
    WEBKIT = "webkit"
    FILETIME = "filetime"
    EXCEL_SERIAL = "excel_serial"
    ISO8601 = "iso8601"
    LOCAL_STRING = "local_string"
    UNKNOWN = "unknown"




class ActivityKind(str, Enum):
    """One real-world happening.

    Deliberately LEAN: NO ``incident`` or ``navigation_search``
    members — an event from a structured list is an entity of ``type=event``, and a
    route/search is expressed by the ``references`` position link, not an activity
    kind.
    """

    CALL = "call"
    MESSAGE = "message"
    TRIP = "trip"
    TRANSACTION = "transaction"
    STAY = "stay"
    OBSERVATION = "observation"
    UNKNOWN = "unknown"


# --- Provenance / record / edit / origin vocabularies ---------------------

class SourceTier(str, Enum):
    """Evidential tier: the original artefact, or a tool's output.

    Primary is first-hand (the acquisition was parsed); secondary is second-hand
    (a tool's export was ingested and its interpretation inherited).
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"


class RecordState(str, Enum):
    """Source-record recovery state (``raw_record.recovery_state``).

    ``LIVE`` (the worked-example demo's term for an intact, in-place row) is
    kept beside the legacy ``INTACT`` synonym. ``RECOVERED`` (e.g. from WAL/freelist)
    is deliberately distinct from ``DELETED`` because their evidential weight differs.
    """

    LIVE = "live"
    INTACT = "intact"
    DELETED = "deleted"
    RECOVERED = "recovered"
    UNKNOWN = "unknown"


class RecoverySource(str, Enum):
    """Where a source record was physically recovered FROM (``raw_record.recovery_source``).

    The finer companion to ``recovery_state`` (which only says live vs recovered): this
    names the exact origin, and the members are ordered by DESCENDING evidential
    reliability, so ``recovery_source`` doubles as the confidence signal (no separate
    column) — a ``freespace`` row (heuristically byte-carved, a possible false positive)
    is weaker evidence than a ``freelist`` row (an intact but unlinked cell), which is
    weaker than a ``wal``/``journal`` row (a row SQLite itself wrote), which is weaker
    than a ``live`` row still in the b-tree. Provenance only — it is deliberately EXCLUDED
    from the L1 digest (like paths / data_source), so it never moves an identity: a claim
    carved from freespace is the SAME claim as a live one, only less certain to have been
    truly recorded. It reaches each observation through the raw_record→observation join, never
    a copied column.
    """

    LIVE = "live"
    WAL = "wal"
    JOURNAL = "journal"
    FREELIST = "freelist"
    FREESPACE = "freespace"
    UNKNOWN = "unknown"


class RecordType(str, Enum):
    """Legacy derived rank label (main vs additional).

    Kept for continuity; note the analysis table stores a free, method-defined
    ``record_rank_type`` label in the run schema, so ``Analysis.record_rank_type`` is
    typed as plain text, not this enum.
    """

    MAIN = "main"
    ADDITIONAL = "additional"


class EditKind(str, Enum):
    """The kind of manual edit recorded in ``edit_log``.

    Evidence protection: L0/L1 rows are NEVER updated in place — only ``NEW_ROW``
    (mint a new occurrence, keep the original); integration/analysis rows may
    ``UPDATE`` in place. There is deliberately NO blank/empty-row kind: a fabricated
    empty row would forge evidence, so the API never creates one.
    """

    UPDATE = "update"
    NEW_ROW = "new_row"


class Origin(str, Enum):
    """Who specified a value: a preset mapping, or an operator argument.

    Used by ``entity.concerns_origin`` and ``observation.entity_origin``. Excluded from
    both evidence digests — operator input never moves a digest.
    """

    PRESET_MAPPED = "preset_mapped"
    OPERATOR_PARAM = "operator_param"


# RelationType / RelationOrigin: DEFERRED with the analyst graph (entity_model.md).
# MentionRole: removed with observation_mention.


# --- Component `part` vocabularies -----------------------------------------
# The standard export names. Any value is ACCEPTED at ingest (the columns are free
# TEXT); only these standard names are export-projected — ``is_standard_part`` is the
# predicate the exporter uses to decide. Spatial parts = the preset target-field
# names; temporal parts = lower/upper/duration plus their split forms (a date column
# and a time column of one bound are two verbatim rows the integration combines).

STANDARD_SPATIAL_PARTS: frozenset[str] = frozenset({
    "raw_position",          # composite / free-text position (one field)
    # The two ANGLE parts are DATUM-AGNOSTIC at L1 — they name "the source's latitude /
    # longitude words, verbatim". The datum (WGS84 / epsg:4326) is carried ONLY by the
    # spatial_expression.referential, never baked into the part name: baking it in would
    # double-state the datum and pre-empt the L2 CRS-adoption interpretation. The canonical
    # WGS84 float columns keep their _wgs84 names at L2 (SpatialIntegration below) — that is
    # the canonical layer, where the datum IS asserted.
    "latitude",
    "longitude",
    "altitude_m",
    "horizontal_accuracy_m",
    "vertical_accuracy_m",
    "horizontal_speed_kmh",
    "vertical_speed_kmh",
    "horizontal_speed_accuracy_kmh",
    "vertical_speed_accuracy_kmh",
    "heading_deg",
    "heading_accuracy_deg",
    "beam_azimuth_deg",
    "beam_width_deg",
    "zone",                  # gridded-CRS trio (MGRS / UTM)
    "easting",
    "northing",
})

STANDARD_TEMPORAL_PARTS: frozenset[str] = frozenset({
    "lower",
    "upper",
    "duration",
    # split forms: a bound spanning a date column + a time column — the
    # integration recombines them (datetime_combine).
    "lower_date",
    "lower_time",
    "upper_date",
    "upper_time",
})

# One combined set for the exporter's convenience: the parts drive different tables
# but share one predicate.
STANDARD_PARTS: frozenset[str] = STANDARD_SPATIAL_PARTS | STANDARD_TEMPORAL_PARTS


def is_standard_part(part: str) -> bool:
    """True when ``part`` is one of the documented export-projected part names.

    Any part value is accepted at ingest (verbatim); only standard names
    are lifted into the flat CSV, so the exporter gates projection on this predicate.
    """
    return part in STANDARD_PARTS


# --- Family dataclasses (one per table) -----------------------------------
# One frozen dataclass per run-DB table, matching the demo DB's column shapes. The
# assembly engine builds these; the store writes them. Frozen because an observation,
# once assembled, is an immutable fact; edits mint new rows (edit_log), never mutate.

@dataclass(frozen=True)
class CaseMetadata:
    """table: case_metadata — the root, exactly one row per DB."""

    case_id: str          # uuid4
    case_number: str      # user-facing identifier
    title: str | None = None
    description: str | None = None
    created_at: str | None = None
    created_by: str | None = None   # who opened the case; the run's own operator is run.examiner


@dataclass(frozen=True)
class Run:
    """table: run — the only non-deterministic id (uuid4); feeds no child id."""

    run_id: str           # uuid4
    case_id: str          # FK -> case_metadata
    input_path: str | None = None       # the operator's --input root
    started_at: str | None = None
    finished_at: str | None = None
    tool_version: str | None = None
    settings_json: str | None = None    # normatively COMPLETE, canonical JSON
    examiner: str | None = None


@dataclass(frozen=True)
class Archive:
    """table: archive — one row per container path."""

    archive_id: str       # uuid5(path)
    path: str             # UNIQUE
    cd_sha256: str        # a plain INDEXED column, never a key (§2c)


@dataclass(frozen=True)
class RunArchive:
    """table: run_archive — the run<->archive link."""

    run_archive_id: str   # uuid5(run_id ⟂ archive_id)
    run_id: str           # FK -> run
    archive_id: str       # FK -> archive
    parsed_at: str | None = None
    skipped: int = 0      # 1 = opt-in, preset-scoped skip, never traceless


@dataclass(frozen=True)
class Source:
    """table: source — what was opened; preset-free; id from paths."""

    source_id: str        # uuid5(input_file_path ⟂ inner_path ⟂ subunit)
    run_id: str           # FK -> run
    input_file_path: str  # outermost artefact opened
    inner_path: str | None = None       # chain-inclusive container path
    data_source: str | None = None      # where the data originally lived; NULL for primary
    format: str | None = None
    subunit: str | None = None
    fingerprint_sha256: str | None = None
    sibling_fingerprints_json: str | None = None
    exhibit_ref: str | None = None
    source_tier: SourceTier | None = None
    record_count: int | None = None


@dataclass(frozen=True)
class Preset:
    """table: preset — the only content-keyed table: key ⟂ version."""

    preset_id: str        # uuid5(key ⟂ version)
    key: str
    name: str | None
    version: str | None
    definition_digest: str  # sha256 of the canonical definition


@dataclass(frozen=True)
class RunPreset:
    """table: run_preset — the run<->preset link."""

    run_preset_id: str    # uuid5(run_id ⟂ preset_id)
    run_id: str           # FK -> run
    preset_id: str        # FK -> preset


@dataclass(frozen=True)
class RawRecord:
    """table: raw_record — the verbatim source row; raw_json is COMPLETE."""

    raw_record_id: str    # uuid5(source_id ⟂ line_no)
    source_id: str        # FK -> source
    line_no: int
    raw_json: str         # the complete source row, canonical JSON
    native_record_uid: str | None = None   # the artefact's own key
    recovery_state: RecordState | None = None
    # WHERE the row was recovered from (live/wal/journal/freelist/freespace) — the fine
    # provenance behind the coarse recovery_state, and the confidence signal by its ordering.
    recovery_source: RecoverySource | None = None


@dataclass(frozen=True)
class Activity:
    """table: activity — one happening; a bonus grouping table.

    ``context_json`` is preset-mapped semi-structured context (callee, caller IMEI,
    recipients), canonical JSON, inside the L1 digest projection.
    """

    activity_id: str      # uuid5(raw_record_id ⟂ activity_ordinal)
    kind: ActivityKind | None = None
    context_json: str | None = None


@dataclass(frozen=True)
class ActivityRecord:
    """table: activity_record — the activity<->record link.

    Ingest writes exactly one row per activity (its own record); an analysis act may
    later attach further records.
    """

    activity_record_id: str   # uuid5(activity_id ⟂ raw_record_id)
    activity_id: str          # FK -> activity
    raw_record_id: str        # FK -> raw_record


@dataclass(frozen=True)
class Entity:
    """table: entity — one lean row.

    ``identifier`` is NORMALISED at ingest (E.164, lowercase email…); NULL for a
    purely typed mention. ``raw_identifier`` is the source's own text, verbatim.
    ``concerns`` is the attributed party fixed at parsing (absorbs linked_entity);
    ``concerns_uid`` is the derived digest of the case-folded/stemmed concerns —
    derived, never a key.
    """

    entity_id: str        # uuid5(observation natural key ⟂ corner tag)
    identifier: str | None            # normalised; None = purely typed mention
    raw_identifier: str | None        # the source's own text, verbatim
    # FREE text, not a closed vocabulary: a preset may name any identifier kind (bssid,
    # iccid, a bespoke register…). ``IdentifierType`` lists the kinds the normaliser knows
    # how to canonicalise; an unrecognised kind is simply never normalised (identifier stays
    # NULL). Typed ``str`` so the derived schema carries NO CHECK on this column.
    identifier_type: str              # names the identifier kind (normalisation applied)
    type: EntityType                  # what the thing is
    concerns: str | None = None
    concerns_origin: Origin | None = None
    concerns_uid: str | None = None


@dataclass(frozen=True)
class SpatialComponent:
    """table: spatial_component — one verbatim row per source field.

    ``part`` uses the preset target-field names; ``unit`` is the source unit the preset
    declared for this value.
    """

    spatial_component_id: str   # uuid5(spatial_expression_id ⟂ ordinal)
    spatial_expression_id: str  # FK -> spatial_expression
    ordinal: int
    part: str                   # from the part vocabulary; any value accepted
    source_field: str | None = None
    raw_value: str | None = None   # VERBATIM
    # The source unit the preset DECLARED for this value (``unit: m/s``). Stored per
    # component, not per run: the same part may be declared in different units by two
    # presets, and a run-level map silently let the last one win. raw_value + unit +
    # the canonical L2 column is a complete, checkable conversion.
    unit: str | None = None


@dataclass(frozen=True)
class SpatialExpression:
    """table: spatial_expression — HEADER only.

    ``referential`` = encoding/CRS; ``spatial_source`` = the mechanism (GNSS, WiFi,
    cell…). The values live in ``spatial_component`` rows. ``referential`` is the SOLE carrier of the datum: the ``latitude`` /
    ``longitude`` component parts are datum-agnostic (the source's words), and only an
    ``epsg:4326`` referential lets L2 adopt them as canonical WGS84 — the CRS adoption is
    the interpretation, never pre-stated by the part name.
    """

    spatial_expression_id: str      # uuid5(observation natural key ⟂ corner tag)
    referential: SpatialReferential
    spatial_source: str | None = None


@dataclass(frozen=True)
class TemporalComponent:
    """table: temporal_component — one verbatim row per source field.

    ``part`` uses the preset field names; ``stated_offset`` (the offset the source stated
    for THIS bound, verbatim TEXT) is per-component.
    """

    temporal_component_id: str   # uuid5(temporal_expression_id ⟂ ordinal)
    temporal_expression_id: str  # FK -> temporal_expression
    ordinal: int
    part: str                    # from the part vocabulary; any value accepted
    stated_offset: str | None = None   # verbatim TEXT
    source_field: str | None = None
    raw_value: str | None = None       # VERBATIM


@dataclass(frozen=True)
class TemporalExpression:
    """table: temporal_expression — HEADER only.

    The stated offsets moved to the component; ``granularity``
    dropped (fuzziness lives in the raw value + integration method).
    """

    temporal_expression_id: str     # uuid5(observation natural key ⟂ corner tag)
    kind: TemporalKind
    referential: TemporalReferential | None = None
    temporal_source: str | None = None   # clock mechanism: NTP, internal_clock…


@dataclass(frozen=True)
class Observation:
    """table: observation — one targeted entity, at one place/time expression.

    ``UNIQUE(raw_record_id, preset_id, output_ordinal)`` — two presets reading one
    record produce distinct observations. Both digests are indexed, repeat legally, and
    are never keys.

    TWO entity FKs. ``entity_id`` is the TARGETED entity — the one under investigation, and
    the one the flat CSV names on its line. ``bearer_entity_id`` is the POSITION BEARER — the
    entity whose position the spatial corner actually states, which for a CDR is the antenna
    and for an ANPR read the camera. Storage therefore orders by *whose position it is* while
    the projection orders by *who is under investigation*: the CSV line still names the phone,
    joining ``entity`` twice, and the antenna survives as a column instead of dissolving into
    a ``cell_ref`` string.

    ``bearer_entity_id`` is non-NULL **iff** ``entity_link.needs_bearer`` — i.e. iff the link
    is not ``SELF``. Both directions are malformed otherwise (a ``self`` row carrying a bearer,
    a ``served_by`` row without one); the invariant is enforced at ingest and by lint, since a
    CHECK cannot reach across columns of different vocabularies.

    The three corner links (``entity_link`` / ``spatial_link`` / ``occupancy``) and the
    statement-level ``attestation`` live here.
    """

    observation_id: str          # uuid5(raw_record_id ⟂ preset key ⟂ output_ordinal)
    raw_record_id: str         # FK -> raw_record
    preset_id: str             # FK -> preset
    output_ordinal: int
    entity_id: str             # FK -> entity: the TARGETED entity (owned, UNIQUE)
    content_hash: str          # scoped to the preset's reading of this file
    claim_digest: str          # preset-free L1 projection
    # FK -> entity: the POSITION BEARER (owned, UNIQUE). NULL iff entity_link is SELF — the
    # entity holds its own position, so no second row is minted.
    bearer_entity_id: str | None = None
    entity_origin: Origin | None = None
    activity_id: str | None = None              # FK -> activity
    spatial_expression_id: str | None = None    # FK (owned, UNIQUE)
    temporal_expression_id: str | None = None   # FK (owned, UNIQUE)
    activity_role: ActivityRole | None = None   # the targeted entity's part in the activity
    entity_link: EntityLink | None = None       # targeted -> bearer: how we know
    spatial_link: SpatialLink | None = None     # targeted -> geometry: how close
    occupancy: Occupancy | None = None          # over the interval: how much of it
    attestation: Attestation | None = None      # who asserts the whole statement
    # When the RECORD was written, as distinct from when the phenomenon happened (the temporal
    # corner). Days apart on a card transaction: authorisation vs settlement posting. Provenance
    # about the record, so — like paths and source attributes — it feeds NEITHER digest: a
    # re-export with a new write time must not move a claim's identity.
    recorded_at: str | None = None
    deleted: RecordState | None = None          # the app's own flag
    multiplicity: int | None = None             # NULL = 1
    source_label: str | None = None             # the one per-claim provenance field
    # A human locator for the ORIGINAL source record (the source's own id column, e.g. a
    # table PK), preset-mapped — so a row traces back to where it came from. On the
    # observation (L1), not the raw_record (L0): the value is preset-resolved, and L0 is
    # preset-free. NULL when the preset maps none (line_no + raw_json still locate it).
    input_record_id: str | None = None
    # The source record's OWN uid (a tool's Item ID, a real UUID column), preset-mapped and
    # verbatim — the sibling of ``input_record_id`` above, and on the observation for the SAME
    # reason: the value is preset-resolved and L0 is preset-free. Different job, though:
    # ``input_record_id`` LOCATES one source record, this one GROUPS the output rows a single
    # source record fanned out into, so every observation of one record repeats it. NULL when the
    # preset maps none (or writes ``source_record_uid: default``) — the export then falls back
    # to the engine-generated ``raw_record_id``. Feeds NO digest: identity comes from the L1
    # projection, and a source's own key is provenance, not claim content.
    source_record_uid: str | None = None


@dataclass(frozen=True)
class SpatialIntegration:
    """table: spatial_integration — the complete derived-spatial block.

    The unit conversion IS the interpretation; raws survive in raw_json. 1:1 with the
    spatial expression in v1 (drop the UNIQUE to allow candidates later).
    """

    spatial_integration_id: str   # uuid5(spatial_expression_id ⟂ method)
    spatial_expression_id: str    # FK -> spatial_expression (UNIQUE in v1)
    method: str                   # rule + version + reference data
    # The expression's encoding and mechanism, carried here so the derived block reads
    # whole: an examiner looking at ONE row sees what the coordinates are, how they were
    # derived (method), what the source stated them in (referential) and how the device
    # obtained them (spatial_source) — without joining back to L1. Written once, by the
    # integration pass, straight from the expression, so the copy cannot drift.
    referential: str | None = None
    spatial_source: str | None = None
    latitude_wgs84: float | None = None
    longitude_wgs84: float | None = None
    altitude_m: float | None = None
    horizontal_accuracy_m: float | None = None
    vertical_accuracy_m: float | None = None
    horizontal_speed_kmh: float | None = None
    vertical_speed_kmh: float | None = None
    horizontal_speed_accuracy_kmh: float | None = None
    vertical_speed_accuracy_kmh: float | None = None
    heading_deg: float | None = None
    heading_accuracy_deg: float | None = None
    beam_azimuth_deg: float | None = None
    beam_width_deg: float | None = None


@dataclass(frozen=True)
class SpatialIndex:
    """table: spatial_index — the S2 leaf-cell index.

    ``s2_cell_id`` is the signed-int64 leaf cell (deliberately non-unique — many
    points share a cell), INDEXED; hierarchy is implicit in the id (range scans).
    """

    spatial_index_id: str        # uuid5(spatial_integration_id)
    spatial_integration_id: str  # FK -> spatial_integration (UNIQUE)
    s2_cell_id: int | None = None   # signed int64 leaf cell
    s2_level: int | None = None


@dataclass(frozen=True)
class TimespanIntegration:
    """table: timespan_integration — canonical UTC µs + the per-bound applied-offset
    projections.

    ``*_unix_utc_us`` = canonical UTC µs; ``*_datetime_iso`` = one offset-tagged local
    ISO string (` N/A` when zone unknown, empty when no instant); ``*_date`` /
    ``*_time`` = the local date / time-of-day (µs). Applied offsets per bound carry
    the ``UTC_OFFSET_UNKNOWN`` assumed-UTC sentinel.
    """

    timespan_integration_id: str   # uuid5(temporal_expression_id ⟂ method)
    temporal_expression_id: str    # FK -> temporal_expression (UNIQUE)
    method: str
    lower_unix_utc_us: int | None = None
    upper_unix_utc_us: int | None = None
    lower_datetime_iso: str | None = None
    upper_datetime_iso: str | None = None
    lower_date: int | None = None
    upper_date: int | None = None
    lower_time: int | None = None
    upper_time: int | None = None
    # Applied offset per bound: a float (known), None (n/a — absolute-UTC source), or
    # UTC_OFFSET_UNKNOWN (a naive timestamp materialised by ASSUMING UTC).
    utc_offset_hours_lower: float | str | None = None
    utc_offset_hours_upper: float | str | None = None
    # Per-bound timing uncertainty (whole µs -> INTEGER columns), split like the offsets
    # above: each bound is an independent reading with its own precision (a start known to
    # the second, an end only to the day). A source-stated value (preset-mapped) wins; the
    # integration Method's own accuracy claim is the fallback. None when neither is present
    # — never invented from a clock's display resolution.
    lower_accuracy_us: int | None = None
    upper_accuracy_us: int | None = None


@dataclass(frozen=True)
class Analysis:
    """table: analysis — the generic, method-attributed verdict table.

    Re-running a changed ranker ADDS rows, never mutates. ``record_rank_type`` is a free,
    method-defined label in the run schema (the demo stores gps_fix / cdr_leg /
    anpr_read), so it is plain text here, not ``RecordType``.
    """

    analysis_id: str      # uuid5(observation_id ⟂ method)
    observation_id: str     # FK -> observation
    method: str
    record_rank: int | None = None
    # The label qualifying the rank (main | additional). Named for what it qualifies —
    # a bare ``record_rank_type`` read as "what kind of record is this", which it never was.
    record_rank_type: str | None = None


@dataclass(frozen=True)
class EditLog:
    """table: edit_log — the manual-edit audit journal.

    ``ordinal`` is a monotonic UNIQUE journal position enabling a full rewind:
    ``UPDATE`` logs per-column old->new; ``NEW_ROW`` logs the FULL row snapshot
    (canonical JSON in ``new_value``). Applying inverses in reverse ordinal
    order restores any earlier state.
    """

    edit_id: str          # uuid4 (an audit event)
    ordinal: int          # monotonic UNIQUE journal position
    table_name: str | None = None
    row_id: str | None = None
    column: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    edit_kind: EditKind | None = None
    edited_by: str | None = None
    edited_at: str | None = None


# --- Sentinels ------------------------------------------------------------
# Sentinel for an applied utc_offset when a naive timestamp was materialised by
# ASSUMING UTC: the instant is recorded, but the true offset is unknown. Distinct
# from None (not applicable) and from a real 0.0, so an examiner sees the assumption
# loudly (the ` N/A` on the datetime_iso).
UTC_OFFSET_UNKNOWN = "N/A"


# --- Type & unit metadata (drives cast inference + unit conversion) -------
# A model field's type is a property of the model, declared once here, so a preset never
# has to restate `cast: float` / `cast: int` on a numeric mapping. ``column_cast`` is keyed
# on the ASSIGNABLE field name — what a preset writes in `to:` (presets/spec.py
# ASSIGNABLE_FIELDS) — because that is exactly the string its one caller
# (presets/resolve.py) passes. For the metric float fields the assignable name happens to
# equal the integration column; for the two ANGLE fields it does NOT (a preset maps the
# datum-agnostic ``latitude`` / ``longitude``, whereas the canonical L2 write columns are
# ``latitude_wgs84`` / ``longitude_wgs84``), and for the integer fields it does NOT either
# (the assignable names are ``multiplicity`` / ``time_accuracy_us_lower`` / ``_upper``,
# whereas the downstream write columns are ``lower_accuracy_us`` / ``upper_accuracy_us``),
# so the set must hold the ASSIGNABLE names or the cast never fires for a field a preset
# can map.

_FLOAT_COLUMNS: frozenset[str] = frozenset({
    "latitude", "longitude", "altitude_m",
    "horizontal_accuracy_m", "vertical_accuracy_m",
    "horizontal_speed_kmh", "vertical_speed_kmh",
    "horizontal_speed_accuracy_kmh", "vertical_speed_accuracy_kmh",
    "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg", "beam_width_deg",
})
# The integer-typed assignable fields a preset may map: multiplicity (on the observation) and
# the two per-bound source-stated timing uncertainties. Keyed on the assignable NAME (see the
# block comment above), so e.g. a source value 2.0 for ``multiplicity`` coerces to int 2.
_INT_COLUMNS: frozenset[str] = frozenset({
    "multiplicity", "time_accuracy_us_lower", "time_accuracy_us_upper"})


def column_cast(column: str) -> type | None:
    """The python type an assignable field's value should be coerced to, or None for free text.

    Used by the preset resolver (``presets/resolve.py``) to apply an implicit cast so a preset
    need not restate ``cast:`` on a numeric mapping. ``column`` is the assignable field name a
    preset maps ``to:`` — hence the sets above are keyed on those names, not the downstream
    write columns.
    """
    if column in _FLOAT_COLUMNS:
        return float
    if column in _INT_COLUMNS:
        return int
    return None


# Canonical unit each metric column is stored in. A preset declares the SOURCE unit
# (`unit: m/s`) and the engine converts to this canonical unit.
CANONICAL_UNIT: dict[str, str] = {
    "altitude_m": "m", "horizontal_accuracy_m": "m", "vertical_accuracy_m": "m",
    "horizontal_speed_kmh": "km/h", "vertical_speed_kmh": "km/h",
    "horizontal_speed_accuracy_kmh": "km/h", "vertical_speed_accuracy_kmh": "km/h",
    "heading_deg": "deg", "heading_accuracy_deg": "deg",
    "beam_azimuth_deg": "deg", "beam_width_deg": "deg",
    # An elapsed span, canonical in seconds — the temporal analogue of ``altitude_m``: the
    # unit is in the NAME, so the preset key ``duration_sec`` is unambiguous while the SOURCE
    # unit stays declared per preset ({from: field(Duration), unit: ms}). Not a component part
    # name: the component stays the source's verbatim digits under the part ``duration``, and
    # the conversion is the L2 start_plus_duration method's job.
    "duration_sec": "s",
}

# Multiplicative factor (source_unit, canonical_unit) -> factor.
_UNIT_FACTORS: dict[tuple[str, str], float] = {
    ("m/s", "km/h"): 3.6,
    ("km/h", "km/h"): 1.0,
    ("mph", "km/h"): 1.609344,
    ("knots", "km/h"): 1.852,
    ("m", "m"): 1.0,
    ("ft", "m"): 0.3048,
    ("deg", "deg"): 1.0,
    # Durations. Operators disclose call lengths in seconds; ms/us appear in app databases and
    # ``d`` in Excel exports, whose day-fraction serials measure spans in days.
    ("s", "s"): 1.0,
    ("ms", "s"): 0.001,
    ("us", "s"): 0.000001,
    ("min", "s"): 60.0,
    ("h", "s"): 3600.0,
    ("d", "s"): 86400.0,
}


def unit_factor(source_unit: str, column: str) -> float:
    """Factor to convert ``source_unit`` into ``column``'s canonical unit.

    Raises ValueError for an unknown column/unit pair so a typo is caught at parse
    time rather than silently producing a wrong magnitude.
    """
    canonical = CANONICAL_UNIT.get(column)
    if canonical is None:
        raise ValueError(f"column {column!r} has no canonical unit; cannot apply unit conversion")
    factor = _UNIT_FACTORS.get((source_unit, canonical))
    if factor is None:
        raise ValueError(f"no conversion from {source_unit!r} to {canonical!r} (for {column!r})")
    return factor
