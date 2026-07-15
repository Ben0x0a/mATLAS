"""The single vocabulary + family declaration for the 21-table run database.

Defines:    every controlled-vocabulary enum (the four link/role vocabularies, the
            corner referentials/kinds, the identifier/entity/activity/edit
            vocabularies), the two derived views (``Genesis``, ``Locates``), the
            component ``part`` vocabularies + ``is_standard_part`` predicate, the
            D37 phrase fragments used by the verbalizer, one frozen dataclass per
            table, and the ``column_cast`` / ``CANONICAL_UNIT`` / ``unit_factor``
            helpers describing the integration columns.
Used by:    the schema builder (DDL + CHECKs derive one-directionally from these
            enums), the assembly/ingest engine (casts + unit conversion), the flat
            CSV projection, and the T16 verbalizer (reads the ``phrase`` fragments).
Depends on: the standard library only (``dataclasses``, ``enum``).

This is the successor to ``model_atlas/model/families.py``. It is authoritative:
the run_db_schema.md TABLE definitions and its decision log (1-41) win over the
doc's older "Proposed families.py" code block wherever they disagree — so roles and
``stated_offset`` live on the COMPONENTS, ``granularity`` is dropped, and the
``archive`` / ``run_archive`` / ``run_preset`` / ``definition_digest`` /
``edit_log.ordinal`` additions are present here.

Derivation discipline (why so many enums but no free strings on typed columns): an
invalid vocabulary value is caught the moment a preset is parsed or the DDL CHECK is
built, never written silently into the run DB. ``Genesis`` and ``Locates`` are the
two exceptions to "declared once, stored once": they are DERIVED views of an edge
(a deterministic function of ``EntityTimeLink`` / ``EntityPositionLink``), so they
can never contradict the edge they derive from and are never stored (decisions 11).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# --- Derived views (never stored) -----------------------------------------
# Genesis and Locates are functions of their edge, so a stored copy could drift out
# of sync with the edge; keeping them derived makes that contradiction impossible.

class Genesis(str, Enum):
    """Derived view of the entity-time edge: how the trace came to exist."""

    PRESENCE = "presence"
    ACTION = "action"
    SYSTEM = "system"
    REPORTED = "reported"
    PLANNED = "planned"
    NEGATED = "negated"   # the exclusion/alibi edge (never_during_interval)
    UNKNOWN = "unknown"


class Locates(str, Enum):
    """Derived view of the entity-position edge: whose position the place carries.

    Decision 11: at/claimed_at -> self, within_range_of/at_fixed_detector ->
    infrastructure, references -> referenced, inferred/unknown -> unknown.
    """

    SELF = "self"
    INFRASTRUCTURE = "infrastructure"
    REFERENCED = "referenced"
    UNKNOWN = "unknown"


# --- The four link / role vocabularies (live on the assertion) -------------
# Each phrased member declares a British-English ``phrase`` fragment beside its
# value (decision 37): the T16 verbalizer assembles one deterministic sentence from
# these fragments, so a member's wording is declared here once and derived nowhere
# else. Negation members must read as negation, REFERENCES as a reference (never
# presence), CLAIMED_AT as a claim.

class EntityTimeLink(str, Enum):
    """Entity-time (E-T) edge: what the clock measured about the entity.

    Carries a ``phrase`` fragment (D37) and derives ``genesis`` (never stored).
    ``NEVER_DURING_INTERVAL`` is the negation/alibi member on this edge (open item 1
    of run_db_schema.md — negation beyond the S-T link — and the shape the T00 demo
    stores on ``assertion.entity_time_link``).
    """

    OBSERVED_AT = "observed_at", "was observed at"        # a sensor registered the entity
    EVENT_AT = "event_at", "acted at"                     # the entity caused an event
    RECORDED_AT = "recorded_at", "had a record written at"  # the system wrote the row (may lag)
    REPORTED_FOR = "reported_for", "was reported to be at"  # a recalled/declared time
    INTENDED_FOR = "intended_for", "was planned for"      # a future/planned time
    NEVER_DURING_INTERVAL = "never_during_interval", "was never present during"  # alibi / exclusion
    UNKNOWN = "unknown", "was at an unknown time"

    def __new__(cls, value: str, phrase: str) -> "EntityTimeLink":
        # HOW: a str-Enum member is its own string value; we attach ``phrase`` as an
        # extra attribute so ``member.value`` stays the bare stored token and
        # ``member.phrase`` is the verbaliser fragment.
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def genesis(self) -> Genesis:
        # Genesis is never stored: it is a deterministic function of the E-T edge, so
        # it can never contradict the edge it derives from.
        return _ETLINK_GENESIS[self]


class EntityPositionLink(str, Enum):
    """Entity-position (E-S) edge: whose location this is / how it is bound.

    Carries a ``phrase`` fragment (D37) and derives ``locates`` (never stored).
    """

    AT = "at", "was at"                                   # the entity's own measured position
    WITHIN_RANGE_OF = "within_range_of", "was within range of"  # inside a covering object's range
    AT_FIXED_DETECTOR = "at_fixed_detector", "was detected at"  # passed a fixed sensor
    REFERENCES = "references", "referred to"             # pointed at the place, not present
    CLAIMED_AT = "claimed_at", "was claimed to be at"    # declared by testimony/message
    INFERRED = "inferred", "was inferred to be at"       # analytically derived
    UNKNOWN = "unknown", "was at an unknown place"

    def __new__(cls, value: str, phrase: str) -> "EntityPositionLink":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def locates(self) -> Locates:
        # Derived, never stored: a derived view can never contradict its edge.
        return _EPLINK_LOCATES[self]


class SpatialTemporalLink(str, Enum):
    """Spatial-temporal (S-T) edge: how the position distributes over the interval.

    Absorbs the former ``Temporal relation`` field — they are one edge. Carries a
    ``phrase`` fragment (D37); ``NEVER_DURING_INTERVAL`` reads as an exclusion.
    """

    INSTANT = "instant", "at"
    CONTINUOUS_DURING_INTERVAL = "continuous_during_interval", "continuously during"
    ONCE_DURING_INTERVAL = "once_during_interval", "once during"
    SPORADIC_DURING_INTERVAL = "sporadic_during_interval", "sporadically during"
    NEVER_DURING_INTERVAL = "never_during_interval", "at no point during"   # an exclusion / alibi
    UNKNOWN_DURING_INTERVAL = "unknown_during_interval", "at an unknown point during"

    def __new__(cls, value: str, phrase: str) -> "SpatialTemporalLink":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


class EntityRole(str, Enum):
    """The entity's part in the activity — the 4th member of the link triptych.

    Carries a ``phrase`` fragment (D37): a noun phrase naming the part played.
    Consolidates the worked-examples ⊕ additions (subject, caller, callee, sender,
    recipient, reporter…).
    """

    SUBJECT = "subject", "the subject"
    CALLER = "caller", "the caller"
    CALLEE = "callee", "the callee"
    SENDER = "sender", "the sender"
    RECIPIENT = "recipient", "the recipient"
    REPORTER = "reporter", "the reporter"
    OWNER = "owner", "the owner"
    UNKNOWN = "unknown", "an entity"

    def __new__(cls, value: str, phrase: str) -> "EntityRole":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


# --- Corner referentials, kinds and their roles ---------------------------

class IdentifierType(str, Enum):
    """Which normalisation produced ``entity.identifier`` (decision 17).

    ``NONE`` = a purely typed mention ('the device'): ``identifier`` stays NULL, so
    nothing can ever converge on it. Consolidates the ⊕ additions camera / case_ref.
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
    EVENT = "event"    # decision 36: a placed event from a structured list
    POI = "poi"        # decision 36: a placed point of interest
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


class SpatialRole(str, Enum):
    """The place's part in the activity (per component — decision 25)."""

    VICINITY = "vicinity"
    ENTRY = "entry"
    EXIT = "exit"
    ORIGIN = "origin"
    DESTINATION = "destination"
    WAYPOINT = "waypoint"
    UNKNOWN = "unknown"


class TemporalKind(str, Enum):
    """Shape of the temporal expression.

    Carries a ``phrase`` fragment (D37): the sentence differs for an instant vs an
    interval, so the verbaliser reads the kind to choose 'at <instant>' or
    'from <lower> to <upper>'.
    """

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


class TemporalRole(str, Enum):
    """The bound's part in the activity (per component — decision 25).

    Each bound carries its own role: a visit's lower is ``check_in``, its upper
    ``check_out``.
    """

    START = "start"
    END = "end"
    BOOKING = "booking"
    CHECK_IN = "check_in"
    CHECK_OUT = "check_out"
    LAST_SEEN = "last_seen"
    UNKNOWN = "unknown"


class ActivityKind(str, Enum):
    """One real-world happening (decision 18/35).

    Deliberately LEAN (decisions 35/36): NO ``incident`` or ``navigation_search``
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

    ``LIVE`` (the T00 demo / worked-examples term for an intact, in-place row) is
    kept beside the legacy ``INTACT`` synonym. ``RECOVERED`` (e.g. from WAL/freelist)
    is deliberately distinct from ``DELETED`` because their evidential weight differs.
    """

    LIVE = "live"
    INTACT = "intact"
    DELETED = "deleted"
    RECOVERED = "recovered"
    UNKNOWN = "unknown"


class RecordType(str, Enum):
    """Legacy derived rank label (main vs additional).

    Kept for continuity; note the analysis table stores a free, method-defined
    ``record_type`` label in the run schema, so ``Analysis.record_type`` is typed as
    plain text, not this enum.
    """

    MAIN = "main"
    ADDITIONAL = "additional"


class EditKind(str, Enum):
    """The kind of manual edit recorded in ``edit_log`` (decisions 34/41).

    Evidence protection: L0/L1 rows are NEVER updated in place — only ``NEW_ROW``
    (mint a new occurrence, keep the original) or ``EMPTY_ROW``; integration/analysis
    rows may ``UPDATE`` in place.
    """

    UPDATE = "update"
    NEW_ROW = "new_row"
    EMPTY_ROW = "empty_row"


class Origin(str, Enum):
    """Who specified a value: a preset mapping, or an operator argument.

    Used by ``entity.concerns_origin`` and ``assertion.entity_origin`` (decisions
    12/17/24). Excluded from both evidence digests — operator input never moves a
    digest.
    """

    PRESET_MAPPED = "preset_mapped"
    OPERATOR_PARAM = "operator_param"


# RelationType / RelationOrigin: DEFERRED with the analyst graph (decision 16,
# entity_model.md). MentionRole: removed with assertion_mention (decision 18).


# --- Derivation tables for the two derived views --------------------------

_ETLINK_GENESIS: dict[EntityTimeLink, Genesis] = {
    EntityTimeLink.OBSERVED_AT: Genesis.PRESENCE,
    EntityTimeLink.EVENT_AT: Genesis.ACTION,
    EntityTimeLink.RECORDED_AT: Genesis.SYSTEM,
    EntityTimeLink.REPORTED_FOR: Genesis.REPORTED,
    EntityTimeLink.INTENDED_FOR: Genesis.PLANNED,
    EntityTimeLink.NEVER_DURING_INTERVAL: Genesis.NEGATED,
    EntityTimeLink.UNKNOWN: Genesis.UNKNOWN,
}

_EPLINK_LOCATES: dict[EntityPositionLink, Locates] = {
    EntityPositionLink.AT: Locates.SELF,
    EntityPositionLink.CLAIMED_AT: Locates.SELF,
    EntityPositionLink.WITHIN_RANGE_OF: Locates.INFRASTRUCTURE,
    EntityPositionLink.AT_FIXED_DETECTOR: Locates.INFRASTRUCTURE,
    EntityPositionLink.REFERENCES: Locates.REFERENCED,
    EntityPositionLink.INFERRED: Locates.UNKNOWN,
    EntityPositionLink.UNKNOWN: Locates.UNKNOWN,
}


# --- Component `part` vocabularies (decision 23) --------------------------
# The standard export names. Any value is ACCEPTED at ingest (the columns are free
# TEXT); only these standard names are export-projected — ``is_standard_part`` is the
# predicate the exporter uses to decide. Spatial parts = the preset target-field
# names; temporal parts = lower/upper/duration plus their split forms (a date column
# and a time column of one bound are two verbatim rows the integration combines).

STANDARD_SPATIAL_PARTS: frozenset[str] = frozenset({
    "raw_position",          # composite / free-text position (one field)
    "latitude_wgs84",
    "longitude_wgs84",
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
    # split forms: a bound spanning a date column + a time column (D15) — the
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

    Any part value is accepted at ingest (verbatim, decision 23); only standard names
    are lifted into the flat CSV, so the exporter gates projection on this predicate.
    """
    return part in STANDARD_PARTS


# --- Family dataclasses (one per table) -----------------------------------
# One frozen dataclass per run-DB table, matching the demo DB's column shapes. The
# assembly engine builds these; the store writes them. Frozen because an assertion,
# once assembled, is an immutable fact; edits mint new rows (edit_log), never mutate.

@dataclass(frozen=True)
class CaseMetadata:
    """table: case_metadata — the root, exactly one row per DB (decision 28)."""

    case_id: str          # uuid4
    case_number: str      # user-facing identifier
    title: str | None = None
    description: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class Run:
    """table: run — the only non-deterministic id (uuid4); feeds no child id."""

    run_id: str           # uuid4
    case_id: str          # FK -> case_metadata
    input_path: str | None = None       # the operator's --input root (decision 24)
    started_at: str | None = None
    finished_at: str | None = None
    tool_version: str | None = None
    settings_json: str | None = None    # normatively COMPLETE, canonical JSON (D39)
    examiner: str | None = None


@dataclass(frozen=True)
class Archive:
    """table: archive — one row per container path (decision 38)."""

    archive_id: str       # uuid5(path)
    path: str             # UNIQUE
    cd_sha256: str        # a plain INDEXED column, never a key (§2c)


@dataclass(frozen=True)
class RunArchive:
    """table: run_archive — the run<->archive link (decision 38)."""

    run_archive_id: str   # uuid5(run_id ⟂ archive_id)
    run_id: str           # FK -> run
    archive_id: str       # FK -> archive
    parsed_at: str | None = None
    skipped: int = 0      # 1 = opt-in, preset-scoped skip, never traceless


@dataclass(frozen=True)
class Source:
    """table: source — what was opened; preset-free (decision 27); id from paths."""

    source_id: str        # uuid5(input_file_path ⟂ inner_path ⟂ subunit)
    run_id: str           # FK -> run
    input_file_path: str  # outermost artefact opened
    inner_path: str | None = None       # chain-inclusive container path
    data_source: str | None = None      # where the data originally lived (D24); NULL for primary
    format: str | None = None
    subunit: str | None = None
    fingerprint_sha256: str | None = None
    sibling_fingerprints_json: str | None = None
    exhibit_ref: str | None = None
    source_tier: SourceTier | None = None
    record_count: int | None = None


@dataclass(frozen=True)
class Preset:
    """table: preset — the only content-keyed table: key ⟂ version (decision 27)."""

    preset_id: str        # uuid5(key ⟂ version)
    key: str
    name: str | None
    version: str | None
    definition_digest: str  # sha256 of the canonical definition (decision 40)


@dataclass(frozen=True)
class RunPreset:
    """table: run_preset — the run<->preset link (decision 39)."""

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
    input_record_id: str | None = None      # human locator
    recovery_state: RecordState | None = None


@dataclass(frozen=True)
class Activity:
    """table: activity — one happening; a bonus grouping table (decision 35).

    ``context_json`` is preset-mapped semi-structured context (callee, caller IMEI,
    recipients), canonical JSON, inside the L1 digest projection.
    """

    activity_id: str      # uuid5(raw_record_id ⟂ activity_ordinal)
    kind: ActivityKind | None = None
    context_json: str | None = None


@dataclass(frozen=True)
class ActivityRecord:
    """table: activity_record — the activity<->record link (decision 26).

    Ingest writes exactly one row per activity (its own record); an analysis act may
    later attach further records.
    """

    activity_record_id: str   # uuid5(activity_id ⟂ raw_record_id)
    activity_id: str          # FK -> activity
    raw_record_id: str        # FK -> raw_record


@dataclass(frozen=True)
class Entity:
    """table: entity — one lean row (decisions 17/21/22).

    ``identifier`` is NORMALISED at ingest (E.164, lowercase email…); NULL for a
    purely typed mention. ``raw_identifier`` is the source's own text, verbatim.
    ``concerns`` is the attributed party fixed at parsing (absorbs linked_entity);
    ``concerns_uid`` is the derived digest of the case-folded/stemmed concerns —
    derived, never a key.
    """

    entity_id: str        # uuid5(assertion natural key ⟂ corner tag)
    identifier: str | None            # normalised; None = purely typed mention
    identifier_type: IdentifierType   # names the normalisation applied
    type: EntityType                  # what the thing is
    concerns: str | None = None
    concerns_origin: Origin | None = None
    concerns_uid: str | None = None


@dataclass(frozen=True)
class SpatialComponent:
    """table: spatial_component — one verbatim row per source field (decision 15/25).

    ``part`` uses the preset target-field names (decision 23); ``spatial_role`` is
    per-component (decision 25).
    """

    spatial_component_id: str   # uuid5(spatial_expression_id ⟂ ordinal)
    spatial_expression_id: str  # FK -> spatial_expression
    ordinal: int
    part: str                   # from the part vocabulary; any value accepted
    spatial_role: SpatialRole | None = None
    source_field: str | None = None
    raw_value: str | None = None   # VERBATIM


@dataclass(frozen=True)
class SpatialExpression:
    """table: spatial_expression — HEADER only (decision 15).

    ``referential`` = encoding/CRS; ``spatial_source`` = the mechanism (GNSS, WiFi,
    cell…). The values live in ``spatial_component`` rows; ``spatial_role`` moved to
    the component (decision 25).
    """

    spatial_expression_id: str      # uuid5(assertion natural key ⟂ corner tag)
    referential: SpatialReferential
    spatial_source: str | None = None


@dataclass(frozen=True)
class TemporalComponent:
    """table: temporal_component — one verbatim row per source field (decision 15/25).

    ``part`` uses the preset field names (decision 23); ``temporal_role`` and
    ``stated_offset`` (the offset the source stated for THIS bound, verbatim TEXT —
    R13) are per-component (decision 25).
    """

    temporal_component_id: str   # uuid5(temporal_expression_id ⟂ ordinal)
    temporal_expression_id: str  # FK -> temporal_expression
    ordinal: int
    part: str                    # from the part vocabulary; any value accepted
    temporal_role: TemporalRole | None = None
    stated_offset: str | None = None   # verbatim TEXT (R13)
    source_field: str | None = None
    raw_value: str | None = None       # VERBATIM


@dataclass(frozen=True)
class TemporalExpression:
    """table: temporal_expression — HEADER only (decision 15).

    ``temporal_role`` and the stated offsets moved to the component; ``granularity``
    dropped (decision 25 — fuzziness lives in the raw value + integration method).
    """

    temporal_expression_id: str     # uuid5(assertion natural key ⟂ corner tag)
    kind: TemporalKind
    referential: TemporalReferential | None = None
    temporal_source: str | None = None   # clock mechanism: NTP, internal_clock…


@dataclass(frozen=True)
class Assertion:
    """table: assertion — one entity, in one role, at one place/time expression.

    ``UNIQUE(raw_record_id, preset_id, output_ordinal)`` — two presets reading one
    record produce distinct assertions. Both digests are indexed, repeat legally, and
    are never keys. The four link/role vocabularies live here (decision 5).
    """

    assertion_id: str          # uuid5(raw_record_id ⟂ preset key ⟂ output_ordinal)
    raw_record_id: str         # FK -> raw_record
    preset_id: str             # FK -> preset (decision 27)
    output_ordinal: int
    entity_id: str             # FK -> entity (owned, UNIQUE)
    content_hash: str          # scoped to the preset's reading of this file
    claim_digest: str          # preset-free L1 projection (R5)
    entity_origin: Origin | None = None
    activity_id: str | None = None              # FK -> activity
    spatial_expression_id: str | None = None    # FK (owned, UNIQUE)
    temporal_expression_id: str | None = None   # FK (owned, UNIQUE)
    entity_role: EntityRole | None = None
    entity_position_link: EntityPositionLink | None = None
    entity_time_link: EntityTimeLink | None = None
    spatial_temporal_link: SpatialTemporalLink | None = None
    deleted: RecordState | None = None          # the app's own flag (R11)
    multiplicity: int | None = None             # NULL = 1
    source_label: str | None = None             # the one per-claim provenance field


@dataclass(frozen=True)
class SpatialIntegration:
    """table: spatial_integration — the complete derived-spatial block (decision 30).

    The unit conversion IS the interpretation; raws survive in raw_json. 1:1 with the
    spatial expression in v1 (drop the UNIQUE to allow candidates later).
    """

    spatial_integration_id: str   # uuid5(spatial_expression_id ⟂ method)
    spatial_expression_id: str    # FK -> spatial_expression (UNIQUE in v1)
    method: str                   # rule + version + reference data
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
    """table: spatial_index — the S2 leaf-cell index (decision 31).

    ``s2_cell_id`` is the signed-int64 leaf cell (deliberately non-unique — many
    points share a cell), INDEXED; hierarchy is implicit in the id (range scans).
    """

    spatial_index_id: str        # uuid5(spatial_integration_id)
    spatial_integration_id: str  # FK -> spatial_integration (UNIQUE)
    s2_cell_id: int | None = None   # signed int64 leaf cell
    s2_level: int | None = None


@dataclass(frozen=True)
class TimespanIntegration:
    """table: timespan_integration — canonical UTC µs + the D32 projections.

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
    time_accuracy_us: int | float | None = None


@dataclass(frozen=True)
class Analysis:
    """table: analysis — the generic, method-attributed verdict table (decision 19).

    Re-running a changed ranker ADDS rows, never mutates. ``record_type`` is a free,
    method-defined label in the run schema (the demo stores gps_fix / cdr_leg /
    anpr_read), so it is plain text here, not ``RecordType``.
    """

    analysis_id: str      # uuid5(assertion_id ⟂ method)
    assertion_id: str     # FK -> assertion
    method: str
    record_type: str | None = None
    record_rank: int | None = None


@dataclass(frozen=True)
class EditLog:
    """table: edit_log — the manual-edit audit journal (decisions 34/41).

    ``ordinal`` is a monotonic UNIQUE journal position enabling a full rewind:
    ``UPDATE`` logs per-column old->new; ``NEW_ROW``/``EMPTY_ROW`` log the FULL row
    snapshot (canonical JSON in ``new_value``). Applying inverses in reverse ordinal
    order restores any earlier state.
    """

    edit_id: str          # uuid4 (an audit event)
    ordinal: int          # monotonic UNIQUE journal position (decision 41)
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
# loudly (the ` N/A` on the datetime_iso, decision 32).
UTC_OFFSET_UNKNOWN = "N/A"


# --- Type & unit metadata (drives cast inference + unit conversion) -------
# A model column's type is a property of the model, declared once here, so a preset
# never has to restate `cast: float` on every numeric mapping. These describe the
# INTEGRATION columns, which is where unit conversion always conceptually belonged.

_FLOAT_COLUMNS: frozenset[str] = frozenset({
    "latitude_wgs84", "longitude_wgs84", "altitude_m",
    "horizontal_accuracy_m", "vertical_accuracy_m",
    "horizontal_speed_kmh", "vertical_speed_kmh",
    "horizontal_speed_accuracy_kmh", "vertical_speed_accuracy_kmh",
    "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg", "beam_width_deg",
})
_INT_COLUMNS: frozenset[str] = frozenset({"time_accuracy_us", "record_rank"})


def column_cast(column: str) -> type | None:
    """The python type a column's value should be coerced to, or None for free text.

    Used by the assembly engine to apply an implicit cast so presets omit ``cast:``.
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
