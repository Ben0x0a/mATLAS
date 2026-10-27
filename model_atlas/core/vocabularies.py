"""The single vocabulary + family declaration for the 21-table run database.

Defines:    every controlled-vocabulary enum (the three stored links, the corner
            referentials/kinds, the
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
them. What replaced it is one stored link per arm of the observation (decision 55): the
entity's role (``EntityLink``), what the observation does with the position
(``SpatialLink``) and how the interval is referenced, negation included (``TemporalLink``).
The fourth link, to the source, is the ``raw_record_id`` chain and has no vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields
from enum import Enum


# --- The links: one per arm of the observation (decision 55) ----------------
# The observation is the centre; each link qualifies ONE arm, at a technical level — how the
# observation is tied to that component, never what the observation is worth (evidential
# weight is a court question). Three are stored columns; the fourth, the source link, is the
# existing ``raw_record_id -> raw_record -> source_file`` chain plus ``preset_id`` and has no
# vocabulary.
#
# Negation lives on ``TemporalLink`` ONLY. Modal duality gives NOT-diamond-P == box-NOT-P, so
# a negation storable on two arms would let one fact be written two ways with no rule for
# disagreement — the bug ``NEVER_DURING_INTERVAL`` once caused by existing on two enums.
#
# Each member declares a British-English ``phrase`` fragment beside its value: the verbalizer
# assembles one deterministic sentence from these fragments, so a member's wording is
# declared here once and derived nowhere else.


class EntityLink(str, Enum):
    """Entity arm — *what role does the targeted entity play in the observation?*

    Also fixes the position BEARER (``observation.bearer_entity_id``, the entity whose
    position the spatial arm states):

    - ``SUBJECT`` — the observation is about the entity's OWN position; the bearer IS the
      targeted entity (same ``entity_id``).
    - ``PARTY`` — the entity deals with ANOTHER entity that holds the position (a CDR's
      antenna, an ATM, a camera, a hotel, a searched POI); the bearer is that other entity.
    - ``MENTIONED`` — the entity is only named by the observation; the bearer is the
      position holder when the record names one, else NULL.
    - ``UNKNOWN`` — the record does not say how the entity relates (a Waze trace); the bearer
      is NULL and none is ever minted, because a placeholder bearer would manufacture a
      presence claim the source does not make.

    Does NOT answer what the observation does with the position (``SpatialLink``), how the
    interval is referenced or whether the claim is negative (``TemporalLink``).
    """

    SUBJECT = "subject", "holding its own position"
    PARTY = "party", "in relation with"
    MENTIONED = "mentioned", "mentioned"
    UNKNOWN = "unknown", "in an undetermined role"

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


class SpatialLink(str, Enum):
    """Spatial arm — *how does the observation use the position?*

    What the record does with the geometry it carries: a position measured for the bearer,
    one a register attributes to it, the place of an antenna that served the entity, a place
    searched for… The verb alone decides whether the record claims contact with the place:
    ``SEARCHED`` / ``SHARED`` / ``CACHED`` do not.

    Does NOT answer: the geometry's precision (the leaf's ``horizontal_accuracy_m`` — the old
    ``at`` / ``within`` distinction), the entity–bearer distance (analyst knowledge, deferred
    in the roadmap — the old ``near``), or negation (``TemporalLink``; there is no negative
    member here).

    OPEN LIST (decision 55): a member is added when a real source needs one, with a comment
    stating what the observation does with the position; the list is reviewed later to
    minimise it. SELECTION RULE: use the verb the source ATTESTS, never one inferred — a
    source that cannot tell ``served`` from ``cached`` is ``UNKNOWN``.
    """

    # The fragments are VERB phrases taking one object: the bearer when another entity holds
    # the position ("was served by <antenna>"), the place otherwise ("was measured at <place>").
    MEASURED = "measured", "was measured at"        # a position measured (GNSS, fingerprint)
    REGISTERED = "registered", "was registered at"  # attributed by a register or lookup (IP geo)
    SERVED = "served", "was served by"              # network infrastructure served the entity
    SEEN = "seen", "was seen by"                    # a sensor OR an observer registered it
    USED = "used", "used"                           # ATM, POS terminal, badge reader
    STAYED = "stayed", "stayed at"                  # hotel, hospital, custody register
    LOCATED = "located", "was located at"           # at a place, as stated (alibi, statement)
    SEARCHED = "searched", "searched for"           # the entity's OWN interest in a place
    SHARED = "shared", "shared"                     # a place sent to or from the entity
    CACHED = "cached", "cached"                     # stored as data; no attested contact
    UNKNOWN = "unknown", "was linked to"            # the use is not determinable

    def __new__(cls, value: str, phrase: str) -> "SpatialLink":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase


class TemporalLink(str, Enum):
    """Temporal arm — *how is the interval referenced, and is the claim positive or negative?*

    A modal quantifier over the stated interval, with P = "the entity relates to the position
    as ``SpatialLink`` says". The four quantified members are the square of opposition:

    - ``THROUGHOUT`` — box P (a GNSS track segment; a hotel stay only if the source says so).
    - ``AT_LEAST_ONCE`` — diamond P (a cache row, a CDR leg, any instant). The weakest
      positive reading: every claim entails it, so it never oversteps.
    - ``NEVER`` — box NOT-P (an alibi: "the device was NOT at home during T").
    - ``NOT_THROUGHOUT`` — diamond NOT-P ("left home at some point during T").

    THE ONLY PLACE NEGATION CAN BE STATED (decision 55); ``EntityLink`` and ``SpatialLink``
    have no negative member, so a negative fact has exactly one encoding. ``NEVER`` /
    ``NOT_THROUGHOUT`` require the source to STATE the absence — a missing record is never
    a negation.

    On an INSTANT (``lower == upper``) box and diamond coincide, so ingest writes ONE
    canonical member: ``THROUGHOUT`` as ``AT_LEAST_ONCE``, ``NOT_THROUGHOUT`` as ``NEVER``
    (see ``canonical_on_instant``).

    Does NOT answer when the interval is (the temporal leaf) or how precisely its bounds are
    known (``timespan_integration.*_accuracy_us``).
    """

    # ``phrase`` introduces an interval ("never during the interval from A to B");
    # ``instant_phrase`` introduces a single instant ("not at T").
    THROUGHOUT = "throughout", "throughout", "at"
    AT_LEAST_ONCE = "at_least_once", "at least once during", "at"
    NEVER = "never", "never during", "not at"
    NOT_THROUGHOUT = "not_throughout", "not throughout", "not at"
    UNKNOWN = "unknown", "for an undetermined part of", "at"

    def __new__(cls, value: str, phrase: str, instant_phrase: str) -> "TemporalLink":
        member = str.__new__(cls, value)
        member._value_ = value
        member._phrase = phrase
        member._instant_phrase = instant_phrase
        return member

    @property
    def phrase(self) -> str:
        return self._phrase

    @property
    def instant_phrase(self) -> str:
        return self._instant_phrase

    @property
    def canonical_on_instant(self) -> "TemporalLink":
        """The ONE member this link is written as on an instant, where box == diamond."""
        return {TemporalLink.THROUGHOUT: TemporalLink.AT_LEAST_ONCE,
                TemporalLink.NOT_THROUGHOUT: TemporalLink.NEVER}.get(self, self)


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


class PositionType(str, Enum):
    """What KIND of locator the source wrote — stated by the preset, never derived.

    The dispatch key for spatial integration: each type has its own mechanism. Only
    ``coordinate`` and ``grid_reference`` take a ``referential``; the rest are self-describing.
    """

    COORDINATE = "coordinate"
    GRID_REFERENCE = "grid_reference"
    STREET_ADDRESS = "street_address"
    CELL_ID = "cell_id"
    BSSID = "bssid"
    UNKNOWN = "unknown"

    @property
    def takes_referential(self) -> bool:
        """Whether a ``referential`` is required — true ONLY for the two typed forms."""
        return self in (PositionType.COORDINATE, PositionType.GRID_REFERENCE)


class SpatialReferential(str, Enum):
    """The CRS or grid the raw place value is expressed in — NOT what kind of locator it is
    (that is ``PositionType``) and NOT its role.

    Narrowed to the systems that actually name a reference frame. Grid references
    (geohash / MGRS / Plus Code) are discrete global grids, not rasters, and are the second
    type that takes one.
    """

    EPSG_4326 = "epsg:4326"
    GEOHASH = "geohash"
    MGRS = "mgrs"
    PLUS_CODE = "plus_code"
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
    route/search is expressed by the ``searched`` spatial link, not an activity kind.
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
    """Source-record recovery state (``raw_record.recovery_state``) — a BINARY fact.

    ``LIVE``: the row was read in place (still in the b-tree). ``RECOVERED``: it was
    reconstructed from somewhere the artefact no longer indexes — WAL, journal, freelist
    or carved free space; ``recovery_source`` names which.

    **Exactly two members, deliberately.** ``INTACT`` was a synonym of ``LIVE``,
    ``UNKNOWN`` a third state no reader could ever produce, and ``DELETED`` duplicated
    ``observation.deleted`` — the APP's own deletion flag, a different fact at a different
    grain (a row the application marked deleted may still be live in the b-tree, and a
    carved row need never have been app-deleted). None of the three was ever written by any
    reader. A vocabulary offering states nothing sets is noise that invites a future author
    to reach for one and mean something new by it.
    """

    LIVE = "live"
    RECOVERED = "recovered"


class RecoverySource(str, Enum):
    """Where a source record was physically recovered FROM (``raw_record.recovery_source``).

    The finer companion to ``recovery_state`` (which only says live vs recovered): this
    names the exact origin, and the members are ordered by DESCENDING evidential
    reliability, so ``recovery_source`` doubles as the confidence signal (no separate
    column) — a ``freespace`` row (heuristically byte-carved, a possible false positive)
    is weaker evidence than a ``freelist`` row (an intact but unlinked cell), which is
    weaker than a ``wal``/``journal`` row (a row SQLite itself wrote).

    **There is no ``live`` member, by design.** A row still in the b-tree was not recovered
    from anywhere, so the column is NULL whenever ``recovery_state`` is LIVE —
    a populated ``recovery_source`` therefore MEANS "this row was recovered", and the two
    columns stop restating one another (39,241 of 42,281 rows on the reference acquisition
    were a pure ``live``/``live`` echo). Provenance only — it is deliberately EXCLUDED
    from the L1 digest (like paths / data_source), so it never moves an identity: a claim
    carved from freespace is the SAME claim as a live one, only less certain to have been
    truly recorded. It reaches each observation through the raw_record→observation join, never
    a copied column.
    """

    WAL = "wal"
    JOURNAL = "journal"
    FREELIST = "freelist"
    FREESPACE = "freespace"
    UNKNOWN = "unknown"


class RecordType(str, Enum):
    """The label stored on ``analysis.record_rank_type``: rank 1 is ``main``, every other
    rank ``additional``. Stored rather than derived (decision 54), so an analyst querying
    the database directly can use it without re-deriving it."""

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

    Used by ``entity.concerns_origin`` (``observation.entity_origin`` was dropped by
    decision 53). Excluded from
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


# --- The mATLAS-enrichment marker ------------------------------------------
# A field is an ENRICHMENT when its value does not come from the trace: a uid mATLAS
# computed, a link a preset author stated, a rank an analysis produced, the operator's own
# case number. A value DERIVED from a trace datum in an obvious way is NOT an enrichment —
# ``latitude_wgs84`` is still the source's own coordinate, just converted, and the
# normalised ``identifier`` is still the source's own number. The test is provenance, not
# whether a transformation happened: ``observation_content_hash`` is derived too, but nothing in the
# trace corresponds to it.
#
# The marker is declared on the FIELD so the model stays authoritative, and it reaches the
# database through the ``model_description`` table (schema.py) — SQLite has no structured
# column attribute, so a described row is the only way a persisted run DB can say which of
# its columns mATLAS added. ``core/flat_view.py`` derives the ``_matlas`` column suffix from
# the same marker.

ENRICHMENT: dict[str, bool] = {"matlas_enrichment": True}


def is_enrichment(dataclass_type: type, field_name: str) -> bool:
    """True when ``field_name`` of ``dataclass_type`` is a mATLAS enrichment.

    The one reader of the marker; schema.py and flat_view.py both call it rather than
    inspecting ``field.metadata`` themselves, so the key is spelled once.
    """
    for member in dataclass_fields(dataclass_type):
        if member.name == field_name:
            return bool(member.metadata.get("matlas_enrichment", False))
    raise KeyError(f"{dataclass_type.__name__} has no field {field_name!r}")


# --- Canonical units, derived from the naming convention -------------------
# The codebase's rule is that a metric column carries its unit IN THE NAME
# (``altitude_m``, ``horizontal_speed_kmh``, ``heading_deg``, ``lower_accuracy_us``), so the
# unit is READ BACK from the suffix rather than restated in a parallel table that could
# drift from the column it describes.

_UNIT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_kmh", "km/h"), ("_deg", "deg"), ("_us", "us"), ("_m", "m"), ("_sec", "s"),
)


def field_unit(field_name: str) -> str | None:
    """The canonical unit a field's NAME declares, or None when it carries no unit.

    Ordered longest-suffix-first so ``horizontal_speed_accuracy_kmh`` resolves to km/h and
    not to the ``_m`` of a shorter match.
    """
    for suffix, unit in _UNIT_SUFFIXES:
        if field_name.endswith(suffix):
            return unit
    return None


# --- Family dataclasses (one per table) -----------------------------------
# One frozen dataclass per run-DB table, matching the demo DB's column shapes. The
# assembly engine builds these; the store writes them. Frozen because an observation,
# once assembled, is an immutable fact; edits mint new rows (edit_log), never mutate.

@dataclass(frozen=True)
class CaseMetadata:
    """table: case_metadata — the root, exactly one row per DB."""

    case_id: str          # uuid4
    case_number: str = field(metadata=ENRICHMENT)   # the operator's own identifier
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
class SourceFile:
    """table: source_file — what was opened; preset-free; id from paths.

    Named for what it identifies: a source is ALWAYS a file (a folder resolves to files, a
    zip to its inner files), so ``source_file`` says what the row points at rather than
    describing its contents. The ER diagram's old "Source metadata" label was the only place
    the word "metadata" appeared, and it named the columns rather than the thing (decision 52).
    """

    source_file_id: str        # uuid5(input_file_path ⟂ inner_path ⟂ subunit)
    run_id: str           # FK -> run
    input_file_path: str  # outermost artefact opened
    inner_path: str | None = None       # chain-inclusive container path
    # Where the source REPORTS the data originally lived — a path a secondary tool's export
    # quotes about the device it parsed. It is the tool's claim, not our observation, hence
    # ``reported_``. NULL for a primary acquisition, where we read the device ourselves and
    # ``input_file_path`` already IS the true location (see pipeline: enforced, not merely
    # left unmapped, so a preset cannot re-introduce a second-hand path we can verify).
    reported_data_source: str | None = None
    format: str | None = None
    subunit: str | None = None
    fingerprint_sha256: str | None = None
    sibling_fingerprints_json: str | None = None
    exhibit_ref: str | None = None
    source_tier: SourceTier | None = field(default=None, metadata=ENRICHMENT)
    record_count: int | None = None


@dataclass(frozen=True)
class Preset:
    """table: preset — the only content-keyed table: key ⟂ version."""

    preset_id: str = field(metadata=ENRICHMENT)   # uuid5(key ⟂ version)
    key: str = field(metadata=ENRICHMENT)
    name: str | None = field(metadata=ENRICHMENT)
    version: str | None = field(metadata=ENRICHMENT)
    definition_digest: str = field(metadata=ENRICHMENT)


@dataclass(frozen=True)
class RunPreset:
    """table: run_preset — the run<->preset link."""

    run_preset_id: str    # uuid5(run_id ⟂ preset_id)
    run_id: str           # FK -> run
    preset_id: str        # FK -> preset


@dataclass(frozen=True)
class RawRecord:
    """table: raw_record — the verbatim source row; raw_json is COMPLETE."""

    raw_record_id: str    # uuid5(source_file_id ⟂ line_no)
    source_file_id: str        # FK -> source
    line_no: int
    raw_json: str         # the complete source row, canonical JSON
    native_record_uid: str | None = None   # the artefact's own key
    # OUR OWN L0 finding: what the READER found when it parsed this file (the sqlite
    # reader's WAL/journal/carve pass). Preset-free, like everything at L0 — one file read
    # by two presets is ONE set of raw_record rows, so nothing preset-resolved may live
    # here. It is stored rather than kept in memory because the pipeline rebuilds
    # ``RawRecord`` from ``raw_json`` in a LATER pass, so this is how the finding survives
    # the pass boundary at all.
    #
    # The ``observation`` columns of the same names are the L1 fact that projects: assemble
    # copies these onto them for a PRIMARY source, and takes the preset's values (i.e. what
    # the tool REPORTS) for a SECONDARY one. Our verified finding and a tool's claim
    # therefore never share a cell — the same discipline as reported_data_source.
    recovery_state: RecordState | None = field(default=None, metadata=ENRICHMENT)
    recovery_source: RecoverySource | None = field(default=None, metadata=ENRICHMENT)


@dataclass(frozen=True)
class Activity:
    """table: activity — one happening. **Deliberately EMPTY in this version.**

    The table is kept so the FK on ``observation.activity_id`` still points somewhere, but
    NOTHING writes it: every activity code path was removed (decision 51) because the one
    thing it was carrying — the antenna a phone was served by — is the POSITION BEARER, and
    D49 gave that a first-class slot. ``context_json`` went with it.

    **Known gap, deliberately open:** non-positional parties (a callee, a chat recipient)
    now have NO home. That is accepted until activity is modelled properly; it must be
    re-opened then, not forgotten.
    """

    activity_id: str      # uuid5(raw_record_id ⟂ activity_ordinal)
    kind: ActivityKind | None = None


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
    # WHAT KIND of locator the source gave — the axis that decides the mechanism. STATED by
    # the preset, never derived, and NOT NULL: every expression has a kind.
    position_type: PositionType
    # The CRS or grid system — REQUIRED for coordinate and grid_reference, NULL for every
    # lookup type, which has nothing to be expressed in.
    referential: SpatialReferential | None = None
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

    Two presets reading one record produce distinct observations, and one preset with
    several observation templates writes several for one record — there is no UNIQUE over
    (raw_record_id, preset_id) for that reason (decision 50). Both digests are indexed,
    repeat legally, and are never keys.

    TWO entity FKs. ``entity_id`` is the TARGETED entity — the one under investigation, and
    the one the flat CSV names on its line. ``bearer_entity_id`` is the POSITION BEARER — the
    entity whose position the spatial corner actually states, which for a CDR is the antenna
    and for an ANPR read the camera. Storage therefore orders by *whose position it is* while
    the projection orders by *who is under investigation*: the CSV line still names the phone,
    joining ``entity`` twice, and the antenna survives as a column instead of dissolving into
    a ``cell_ref`` string.

    ``bearer_entity_id`` follows ``entity_link`` (decision 55): equal to ``entity_id`` for
    ``subject`` (the entity holds its own position), another entity for ``party``, NULL for
    ``unknown`` (no placeholder is ever minted), either for ``mentioned``. Table-level CHECKs
    in the schema enforce it, written with ``IS`` so a NULL cannot slip through.

    The three stored links each qualify one arm: ``entity_link`` the entity, ``spatial_link``
    the position, ``temporal_link`` the interval (and carries the only negation). The fourth
    link, to the source, is ``raw_record_id`` + ``preset_id``.
    """

    # Every id and digest below is a mATLAS enrichment: nothing in the trace corresponds to
    # them, even though they are DERIVED from it — that is the test, not whether a
    # transformation happened.
    observation_id: str = field(metadata=ENRICHMENT)   # uuid5(record ⟂ preset key ⟂ ordinal)
    raw_record_id: str = field(metadata=ENRICHMENT)    # FK -> raw_record
    preset_id: str = field(metadata=ENRICHMENT)        # FK -> preset
    entity_id: str = field(metadata=ENRICHMENT)        # FK -> entity: the TARGETED entity
    observation_content_hash: str = field(metadata=ENRICHMENT)     # this preset's reading of this file
    spatiotemporal_entity_hash: str = field(metadata=ENRICHMENT)     # preset-free L1 projection
    # FK -> entity: the POSITION BEARER. Equal to entity_id when entity_link is `subject`;
    # another owned entity row for `party`; NULL for `unknown` (see the class docstring).
    bearer_entity_id: str | None = field(default=None, metadata=ENRICHMENT)
    activity_id: str | None = field(default=None, metadata=ENRICHMENT)
    spatial_expression_id: str | None = field(default=None, metadata=ENRICHMENT)
    temporal_expression_id: str | None = field(default=None, metadata=ENRICHMENT)
    # The three stored links, one per arm; preset-stated, hence enrichment.
    entity_link: EntityLink | None = field(default=None, metadata=ENRICHMENT)
    spatial_link: SpatialLink | None = field(default=None, metadata=ENRICHMENT)
    temporal_link: TemporalLink | None = field(default=None, metadata=ENRICHMENT)
    # HOW this record was obtained. On a PRIMARY source these come from our own parsing
    # (the sqlite reader's WAL/journal/carve pass); on a SECONDARY source they come from
    # the preset, i.e. from what the tool's export reports — strictly tier-gated, so the
    # two never mix in one cell. They replace the old ``deleted`` column, which asked a
    # question the source could not reliably answer separately from this one.
    # ENRICHMENT even on a secondary source: the stored token is mATLAS's own vocabulary
    # (live / recovered), never the tool's words — a preset LOOKS UP "Intact"/"Carving" into
    # it. The classification is ours, so the projection suffixes it, exactly as it did when
    # the column lived on raw_record.
    recovery_state: RecordState | None = field(default=None, metadata=ENRICHMENT)
    recovery_source: RecoverySource | None = field(default=None, metadata=ENRICHMENT)
    # Preset-stated, so an enrichment: nothing in the trace says "iOS cache".
    source_label: str | None = field(default=None, metadata=ENRICHMENT)
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
    # The VERBATIM information that ALLOWED latitude_wgs84 / longitude_wgs84, as
    # `<source column>:<value> | ...`. Scoped to what the producing METHOD read, not to the
    # latitude/longitude parts: those coincide only while reprojection is the sole
    # mechanism, and a geocoder reads an address, a register lookup a cell or BSSID id.
    # Never the expression's other components — an altitude helped compute nothing.
    position_raw: str | None = None
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
    s2_cell_id: int | None = field(default=None, metadata=ENRICHMENT)  # signed int64 leaf
    s2_level: int | None = field(default=None, metadata=ENRICHMENT)


@dataclass(frozen=True)
class TimespanIntegration:
    """table: timespan_integration — canonical UTC µs + the per-bound offset TO local.

    ``*_unix_utc_us`` is the canonical instant and is ALWAYS UTC. Nothing here renders a
    clock: the six ``*_datetime_iso`` / ``*_date`` / ``*_time`` cells were removed because
    a rendered local string is a presentation concern, and storing it froze one zone into
    the evidence. ``core.local_time`` renders on demand from these two columns instead.

    ``offset_to_local_hours_lower/_upper`` is the offset the ANALYST adds to the canonical
    µs to read that bound in the case's configured ``local_zone`` — DST-resolved at that
    bound's own instant, which is why it is per bound and not a run-level constant. It says
    NOTHING about the source's own clock; the source's offset lives in the verbatim raw
    value and its column name, and HOW the instant was reached is stamped in ``method``
    (``assume_utc`` / ``tz_localize`` / ``datetime_combine``).

    **Do not "restore" the applied-offset meaning.** Until 2026-09-07 this column held the
    offset CONSUMED to reach UTC, which made it NULL for every epoch source — all 42,277
    integrated rows on the reference acquisition — so the configured zone reached the
    analyst on no row at all. The rename is the guard against that reading coming back.
    """

    timespan_integration_id: str   # uuid5(temporal_expression_id ⟂ method)
    temporal_expression_id: str    # FK -> temporal_expression (UNIQUE)
    method: str
    # The expression's shape, encoding and clock, carried here so the derived block reads
    # whole — the temporal twin of spatial_integration's referential/spatial_source. One
    # row states what the instants are, how they were derived (method), what shape the
    # source expressed (kind), in what format (referential) and off which clock
    # (temporal_source), with no join back to L1. Written once, by the integration pass,
    # straight from the expression, so the copy cannot drift.
    kind: str | None = None
    referential: str | None = None
    temporal_source: str | None = None
    lower_unix_utc_us: int | None = None
    upper_unix_utc_us: int | None = None
    # Offset TO the case's local zone per bound: a float (a zone is configured, resolved
    # DST-aware at that bound), LOCAL_OFFSET_UNKNOWN (no local_zone configured — the run
    # cannot say), or None (that bound has no instant to offset).
    offset_to_local_hours_lower: float | str | None = None
    offset_to_local_hours_upper: float | str | None = None
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

    Re-running a changed ranker ADDS rows, never mutates. ``record_rank_type`` is the
    ``RecordType`` label (main | additional) for the rank beside it.
    """

    analysis_id: str      # uuid5(observation_id ⟂ method)
    observation_id: str     # FK -> observation
    method: str
    # The rank within a same-second group, and its label.
    #
    # ``record_rank_type`` IS stored (decision 54), reversing an earlier call to derive it.
    # The label is exactly ``record_rank == 1``, so a stored copy CAN contradict the rank
    # beside it — but this database is read directly, in SQL, by analysts who never run our
    # projection, and a value they cannot query is a value they do not have. The consistency
    # risk is handled by the lint, not by withholding the column.
    record_rank: int | None = field(default=None, metadata=ENRICHMENT)
    record_rank_type: RecordType | None = field(default=None, metadata=ENRICHMENT)


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



# --- Model self-description -------------------------------------------------
# One description per TABLE and per FIELD, feeding the ``model_description`` table so a
# persisted run DB explains itself without this source tree beside it.
#
# WHY an explicit map rather than metadata on every field: these are prose sentences, and
# inlining 181 of them would bury the declarations they describe. This follows the precedent
# ``schema.py`` already sets with ``_table_specs()`` — an explicit map kept beside the
# dataclasses it augments, made drift-proof by an ASSERTION rather than by physical
# adjacency. ``_assert_descriptions_complete()`` runs at IMPORT, so adding a field without
# describing it (or describing one that no longer exists) fails the build immediately
# instead of shipping a database whose self-description has quietly gone stale.
#
# The key is ``(dataclass name, field name)``; a field of ``None`` describes the TABLE.

DESCRIPTIONS: dict[tuple[str, str | None], str] = {
    # -- CaseMetadata --
    ("CaseMetadata", None):
        "The DB root: exactly one row per file, one case per database.",
    ("CaseMetadata", "case_id"):
        "Surrogate uuid4 primary key; the root of the case-run-source-record-observation hierarchy.",
    ("CaseMetadata", "case_number"):
        "The operator's own case reference, as used outside the tool.",
    ("CaseMetadata", "title"):
        "Short human title for the case.",
    ("CaseMetadata", "description"):
        "Free-text description of the case.",
    ("CaseMetadata", "created_at"):
        "When the case row was opened, ISO-8601.",
    ("CaseMetadata", "created_by"):
        "Who opened the case; the run's own operator is run.examiner.",
    # -- Run --
    ("Run", None):
        "One processing run over one input; reproducible from settings_json.",
    ("Run", "run_id"):
        "Surrogate uuid4; the only non-deterministic id, and it feeds no child id so two runs over the same evidence stay diffable.",
    ("Run", "case_id"):
        "The case this run belongs to.",
    ("Run", "input_path"):
        "The operator's --input root for this run.",
    ("Run", "started_at"):
        "Run start, ISO-8601.",
    ("Run", "finished_at"):
        "Run end, ISO-8601.",
    ("Run", "tool_version"):
        "mATLAS version that produced the run.",
    ("Run", "settings_json"):
        "Every operator argument and option, canonical JSON; normatively COMPLETE so the run is reproducible from this row.",
    ("Run", "examiner"):
        "The operator who ran it.",
    # -- Archive --
    ("Archive", None):
        "One container path encountered; identity is the path, never the bytes.",
    ("Archive", "archive_id"):
        "uuid5 of the path alone, so identical bytes at two paths stay two rows.",
    ("Archive", "path"):
        "The container's path, unique.",
    ("Archive", "cd_sha256"):
        "Central-directory digest: an INDEXED column, never a key, so 'same archive twice' is a GROUP BY finding rather than a merge.",
    # -- RunArchive --
    ("RunArchive", None):
        "The run-to-archive link: every encounter recorded, including a skip.",
    ("RunArchive", "run_archive_id"):
        "uuid5(run_id, archive_id).",
    ("RunArchive", "run_id"):
        "The run that opened the archive.",
    ("RunArchive", "archive_id"):
        "The archive opened.",
    ("RunArchive", "parsed_at"):
        "When this run parsed the archive.",
    ("RunArchive", "skipped"):
        "1 when re-walking was skipped by opt-in; never traceless, so a skip is still recorded.",
    # -- Source --
    ("SourceFile", None):
        "One artefact opened: a file or sub-unit, preset-free.",
    ("SourceFile", "source_file_id"):
        "uuid5 of the paths, never the fingerprint, so two byte-identical exhibits stay two sources.",
    ("SourceFile", "run_id"):
        "The run that opened it.",
    ("SourceFile", "input_file_path"):
        "The outermost artefact opened.",
    ("SourceFile", "inner_path"):
        "Chain-inclusive path inside the container.",
    ("SourceFile", "reported_data_source"):
        "Where the source REPORTS the data originally lived — a secondary tool's claim about the device it parsed, not our own observation. NULL for a primary acquisition, where input_file_path already is the true location.",
    ("SourceFile", "format"):
        "The reader that opened it (sqlite, csv, xml...).",
    ("SourceFile", "subunit"):
        "The unit within the file, e.g. a table or sheet.",
    ("SourceFile", "fingerprint_sha256"):
        "Digest of the main file bytes; INDEXED, never a key.",
    ("SourceFile", "sibling_fingerprints_json"):
        "Digests of the sidecar files (WAL, SHM) as canonical JSON.",
    ("SourceFile", "exhibit_ref"):
        "The exhibit reference this artefact came from.",
    ("SourceFile", "source_tier"):
        "primary (an acquisition was parsed) or secondary (a tool's export was parsed and its interpretation inherited).",
    ("SourceFile", "record_count"):
        "How many records were read from it.",
    # -- Preset --
    ("Preset", None):
        "One preset definition applied in this run; the only content-keyed table.",
    ("Preset", "preset_id"):
        "uuid5(key, version): a preset's definition IS its identity.",
    ("Preset", "key"):
        "Stable preset key, unique with version.",
    ("Preset", "name"):
        "Human-readable preset name.",
    ("Preset", "version"):
        "Preset version.",
    ("Preset", "definition_digest"):
        "sha256 of the canonical definition; catches an edit the author forgot to version-bump.",
    # -- RunPreset --
    ("RunPreset", None):
        "The run-to-preset link: every preset SELECTED, even one that yielded nothing.",
    ("RunPreset", "run_preset_id"):
        "uuid5(run_id, preset_id).",
    ("RunPreset", "run_id"):
        "The run.",
    ("RunPreset", "preset_id"):
        "The preset selected.",
    # -- RawRecord --
    ("RawRecord", None):
        "One verbatim source row, preserved complete and untransformed (L0).",
    ("RawRecord", "raw_record_id"):
        "uuid5(source_file_id, line_no).",
    ("RawRecord", "source_file_id"):
        "The artefact it was read from.",
    ("RawRecord", "line_no"):
        "Position within the source, unique per source.",
    ("RawRecord", "raw_json"):
        "The COMPLETE source row as canonical JSON; nothing is dropped here.",
    ("RawRecord", "native_record_uid"):
        "The artefact's own key for the row, if it has one.",
    # -- Activity --
    ("Activity", None):
        "One real-world happening; a grouping table only, never the sole home of anything exploitable.",
    ("Activity", "activity_id"):
        "uuid5(raw_record_id, activity_ordinal).",
    ("Activity", "kind"):
        "What happened: call, message, trip, transaction, stay, observation.",
    # -- Entity --
    ("Entity", None):
        "Who or what: one lean mention row, never deduplicated.",
    ("Entity", "entity_id"):
        "uuid5(observation natural key, role tag): a per-observation occurrence, so the same antenna in 500 rows is 500 rows.",
    ("Entity", "identifier"):
        "The identifier NORMALISED at ingest (E.164, lowercase email); NULL for a purely typed mention. Convergence is GROUP BY (identifier_type, identifier), never a stored merge.",
    ("Entity", "raw_identifier"):
        "The source's own text for the identifier, verbatim.",
    ("Entity", "identifier_type"):
        "Which identifier kind, and so which normalisation applied; free text, since a preset may name any kind.",
    ("Entity", "type"):
        "What the thing IS: device, person, vehicle, account, camera, antenna, event, poi.",
    ("Entity", "concerns"):
        "The attributed party fixed at parsing.",
    ("Entity", "concerns_origin"):
        "Whether concerns came from a preset mapping or an operator argument.",
    ("Entity", "concerns_uid"):
        "Digest of the case-folded, stemmed concerns; derived, never a key.",
    # -- SpatialExpression --
    ("SpatialExpression", None):
        "The place AS THE SOURCE EXPRESSED IT: header only, values live in the components.",
    ("SpatialExpression", "spatial_expression_id"):
        "uuid5(observation natural key, corner tag); owned 1:1 by its observation.",
    ("SpatialExpression", "position_type"):
        "WHAT KIND of locator the source gave, and so which mechanism reaches WGS84: coordinate (reprojection or decoding, no external data - the only type supported today), street_address (geocoding), cell_id (register lookup, GCI only), bssid (location service), place_name (gazetteer centroid). Split out of `referential`, which was answering this and 'in what CRS' at once.",
    ("SpatialExpression", "referential"):
        "The CRS or encoding of a COORDINATE, and the SOLE carrier of the datum - meaningful ONLY when position_type is coordinate. Adoption as canonical WGS84 requires BOTH: type coordinate and referential epsg:4326. Every other type reaches WGS84 by a lookup and carries no referential.",
    ("SpatialExpression", "spatial_source"):
        "The positioning mechanism the source named: GNSS, WiFi, cell.",
    # -- SpatialComponent --
    ("SpatialComponent", None):
        "One verbatim place field as the source wrote it.",
    ("SpatialComponent", "spatial_component_id"):
        "uuid5(spatial_expression_id, ordinal).",
    ("SpatialComponent", "spatial_expression_id"):
        "The expression it belongs to.",
    ("SpatialComponent", "ordinal"):
        "Position within the expression; the tuple order the digest hashes.",
    ("SpatialComponent", "part"):
        "Which part of the place this value is (latitude, longitude, raw_position, altitude_m...); any value is accepted, only standard names are export-projected.",
    ("SpatialComponent", "source_field"):
        "The source column the value came from; excluded from the digests, so a renamed column cannot move a claim's identity.",
    ("SpatialComponent", "raw_value"):
        "The source's value, VERBATIM.",
    ("SpatialComponent", "unit"):
        "The source unit the preset declared for THIS value; per component, so two presets declaring different units for one part cannot collide.",
    # -- TemporalExpression --
    ("TemporalExpression", None):
        "The time AS THE SOURCE EXPRESSED IT: header only, values live in the components.",
    ("TemporalExpression", "temporal_expression_id"):
        "uuid5(observation natural key, corner tag); owned 1:1 by its observation.",
    ("TemporalExpression", "kind"):
        "Shape of the expression: instant, interval, or an open bound. An instant is the degenerate interval where lower equals upper.",
    ("TemporalExpression", "referential"):
        "Format or epoch of the raw value: unix_epoch_s, apple_cfabsolute, iso8601, excel_serial.",
    ("TemporalExpression", "temporal_source"):
        "The clock mechanism the source named: NTP, internal_clock.",
    # -- TemporalComponent --
    ("TemporalComponent", None):
        "One verbatim time field as the source wrote it.",
    ("TemporalComponent", "temporal_component_id"):
        "uuid5(temporal_expression_id, ordinal).",
    ("TemporalComponent", "temporal_expression_id"):
        "The expression it belongs to.",
    ("TemporalComponent", "ordinal"):
        "Position within the expression; the tuple order the digest hashes.",
    ("TemporalComponent", "part"):
        "Which bound this value is (lower, upper, duration, or a date/time split).",
    ("TemporalComponent", "stated_offset"):
        "The UTC offset the SOURCE stated for this bound, verbatim text; distinct from the offset L2 applied.",
    ("TemporalComponent", "source_field"):
        "The source column the value came from; excluded from the digests.",
    ("TemporalComponent", "raw_value"):
        "The source's value, VERBATIM.",
    # -- Observation --
    ("Observation", None):
        "One targeted entity, at one place expression and one time expression, qualified by one link per arm (L1).",
    ("Observation", "observation_id"):
        "A fresh uuid4 (decision 50): an id distinguishes rows, it does not order them and does not detect duplicates — spatiotemporal_entity_hash does that, and works across files.",
    ("Observation", "raw_record_id"):
        "The verbatim record this claim was read from.",
    ("Observation", "preset_id"):
        "Which preset's reading produced it; two presets over one record give two observations.",
    ("Observation", "entity_id"):
        "The TARGETED entity: the one under investigation, and the one the flat CSV names on its line.",
    ("Observation", "observation_content_hash"):
        "Digest scoped to this preset's reading of this file; repeats legally, never a key.",
    ("Observation", "spatiotemporal_entity_hash"):
        "Preset-free and file-free digest of the L1 projection, so two disclosures of one claim converge as a possible-same-evidence finding.",
    ("Observation", "bearer_entity_id"):
        "The POSITION BEARER: the entity whose position the spatial arm states. Equal to entity_id for subject, another entity for party (a CDR's antenna, an ANPR's camera), NULL for unknown.",
    ("Observation", "activity_id"):
        "Reserved for the happening this observation is a leg of. ALWAYS NULL in this version: activity is not modelled yet (decision 51), and the FK is kept so the link exists when it is.",
    ("Observation", "spatial_expression_id"):
        "The place corner, owned 1:1; NULL when the record states no place.",
    ("Observation", "temporal_expression_id"):
        "The time corner, owned 1:1; NULL when the record states no time.",
    ("Observation", "entity_link"):
        "ENTITY ARM: the targeted entity's role in the observation (subject = it holds the position, party = another entity holds it, mentioned, unknown). Fixes the bearer.",
    ("Observation", "spatial_link"):
        "SPATIAL ARM: what the observation does with the position (measured, registered, served, seen, used, stayed, located, searched, shared, cached, unknown). No negative member.",
    ("Observation", "temporal_link"):
        "TEMPORAL ARM: how the interval is referenced (throughout, at_least_once, never, not_throughout, unknown). The ONLY place negation is stated; on an instant only at_least_once / never are written.",
    ("RawRecord", "recovery_state"):
        "What OUR parsing found for this record: read in place (live) or reconstructed (recovered). Preset-free L0; the observation column of the same name is what projects.",
    ("RawRecord", "recovery_source"):
        "Where OUR parsing recovered this record from (wal, journal, freelist, freespace), ordered by descending reliability; NULL when the record was live.",
    ("Observation", "recovery_state"):
        "Whether this record was read in place (live) or reconstructed from where the artefact no longer indexes it (recovered). From our own parsing on a primary source, from the preset (i.e. the tool's report) on a secondary one.",
    ("Observation", "recovery_source"):
        "Where a recovered record was physically recovered from, ordered by DESCENDING reliability (wal, journal, freelist, freespace) so it doubles as the confidence signal; NULL when the record was live, i.e. recovered from nowhere.",
    ("Observation", "source_label"):
        "The one per-claim provenance label, preset-stated.",
    ("Observation", "input_record_id"):
        "A human locator back to the original source record, preset-mapped; LOCATES one record.",
    ("Observation", "source_record_uid"):
        "The source's own record key, preset-mapped; GROUPS the rows one record fanned out into.",
    # -- SpatialIntegration --
    ("SpatialIntegration", None):
        "The place as mATLAS DERIVED it: canonical WGS84 and metrics, method-stamped (L2).",
    ("SpatialIntegration", "spatial_integration_id"):
        "uuid5(spatial_expression_id, method).",
    ("SpatialIntegration", "spatial_expression_id"):
        "The expression this reading derives from; private to it, never shared.",
    ("SpatialIntegration", "position_raw"):
        "The VERBATIM information that allowed latitude_wgs84 and longitude_wgs84, as '<source column>:<value>' joined by ' | ', so a canonical coordinate can be checked against the exact source words behind it. Scoped to what the PRODUCING METHOD read, not to the latitude/longitude components: a reprojection reads the two angles, a geocoder an address, a cell-register or BSSID lookup an identifier. Nothing in mATLAS parses this column - splitting it and comparing against the coordinates is an analyst's check.",
    ("SpatialIntegration", "method"):
        "Rule, version and reference data that produced it; a pass-through is a method too.",
    ("SpatialIntegration", "referential"):
        "The expression's referential, carried onto the derived reading.",
    ("SpatialIntegration", "spatial_source"):
        "The expression's positioning mechanism, carried onto the derived reading.",
    ("SpatialIntegration", "latitude_wgs84"):
        "Canonical WGS84 latitude; the CRS adoption is the interpretation, licensed by the expression's referential.",
    ("SpatialIntegration", "longitude_wgs84"):
        "Canonical WGS84 longitude.",
    ("SpatialIntegration", "altitude_m"):
        "Altitude.",
    ("SpatialIntegration", "horizontal_accuracy_m"):
        "Stated horizontal uncertainty: instrument error, never an epistemic doubt.",
    ("SpatialIntegration", "vertical_accuracy_m"):
        "Stated vertical uncertainty.",
    ("SpatialIntegration", "horizontal_speed_kmh"):
        "Horizontal speed, converted to the canonical unit.",
    ("SpatialIntegration", "vertical_speed_kmh"):
        "Vertical speed, converted to the canonical unit.",
    ("SpatialIntegration", "horizontal_speed_accuracy_kmh"):
        "Stated uncertainty on the horizontal speed.",
    ("SpatialIntegration", "vertical_speed_accuracy_kmh"):
        "Stated uncertainty on the vertical speed.",
    ("SpatialIntegration", "heading_deg"):
        "Direction of travel.",
    ("SpatialIntegration", "heading_accuracy_deg"):
        "Stated uncertainty on the heading.",
    ("SpatialIntegration", "beam_azimuth_deg"):
        "Sector antenna bearing: a cell sector is a wedge, not a disc.",
    ("SpatialIntegration", "beam_width_deg"):
        "Sector antenna beam width.",
    # -- SpatialIndex --
    ("SpatialIndex", None):
        "The S2 cell index over an integrated point, for spatial range queries.",
    ("SpatialIndex", "spatial_index_id"):
        "uuid5(spatial_integration_id); 1:1 with the integration.",
    ("SpatialIndex", "spatial_integration_id"):
        "The derived point this cell indexes.",
    ("SpatialIndex", "s2_cell_id"):
        "Signed int64 S2 LEAF cell, deliberately non-unique since many points share a cell; hierarchy is implicit in the id, so range scans need no parent table.",
    ("SpatialIndex", "s2_level"):
        "The S2 level of the stored cell.",
    # -- TimespanIntegration --
    ("TimespanIntegration", None):
        "The time as mATLAS DERIVED it: canonical UTC microseconds plus local projections, method-stamped (L2).",
    ("TimespanIntegration", "timespan_integration_id"):
        "uuid5(temporal_expression_id, method).",
    ("TimespanIntegration", "temporal_expression_id"):
        "The expression this reading derives from; private to it, never shared.",
    ("TimespanIntegration", "method"):
        "Rule, version and tz database that produced it.",
    ("TimespanIntegration", "kind"):
        "The expression's shape, carried onto the derived reading.",
    ("TimespanIntegration", "referential"):
        "The expression's epoch or format, carried onto the derived reading.",
    ("TimespanIntegration", "temporal_source"):
        "The expression's clock mechanism, carried onto the derived reading.",
    ("TimespanIntegration", "lower_unix_utc_us"):
        "Canonical lower bound, UTC microseconds.",
    ("TimespanIntegration", "upper_unix_utc_us"):
        "Canonical upper bound, UTC microseconds; equals the lower bound for an instant.",
    ("TimespanIntegration", "offset_to_local_hours_lower"):
        "Hours to ADD to the lower bound's UTC microseconds to read it in the case's configured local zone, resolved for DST at that instant; 'N/A' means no local zone is configured. It does NOT state the source's own offset.",
    ("TimespanIntegration", "offset_to_local_hours_upper"):
        "Hours to add to the upper bound's UTC microseconds to read it in the case's configured local zone, resolved for DST at that instant.",
    ("TimespanIntegration", "lower_accuracy_us"):
        "Timing uncertainty on the lower bound; a source-stated value wins, the method's own claim is the fallback, and neither is invented from a clock's display resolution.",
    ("TimespanIntegration", "upper_accuracy_us"):
        "Timing uncertainty on the upper bound.",
    # -- Analysis --
    ("Analysis", None):
        "A method-attributed verdict ABOUT an observation; re-running adds rows, never mutates (L3).",
    ("Analysis", "analysis_id"):
        "uuid5(observation_id, method).",
    ("Analysis", "observation_id"):
        "The observation judged.",
    ("Analysis", "method"):
        "The analysis method and version that produced the verdict.",
    ("Analysis", "record_rank_type"):
        "The rank's label: main for rank 1, additional otherwise. Stored so SQL against this database can use it without re-deriving it.",
    ("Analysis", "record_rank"):
        "Rank within a same-second group. The main/additional label is NOT stored: it is exactly rank == 1, so a stored copy could only contradict the rank beside it.",
    # -- EditLog --
    ("EditLog", None):
        "The manual-edit audit journal; a full rewind is possible by applying inverses in reverse ordinal order.",
    ("EditLog", "edit_id"):
        "uuid4: an audit event has no natural key.",
    ("EditLog", "ordinal"):
        "Monotonic unique journal position, which is what makes the rewind totally ordered.",
    ("EditLog", "table_name"):
        "The table edited.",
    ("EditLog", "row_id"):
        "The row edited.",
    ("EditLog", "column"):
        "The column edited, for an in-place update.",
    ("EditLog", "old_value"):
        "Value before the edit.",
    ("EditLog", "new_value"):
        "Value after the edit; for a new row, the FULL row snapshot as canonical JSON.",
    ("EditLog", "edit_kind"):
        "update (integration and analysis rows only) or new_row. Evidence protection: L0 and L1 rows are NEVER updated in place.",
    ("EditLog", "edited_by"):
        "Who made the edit.",
    ("EditLog", "edited_at"):
        "When the edit was made.",
}

# The models the description map must cover, in schema order.
_DESCRIBED_MODELS: tuple[type, ...] = (
    CaseMetadata, Run, Archive, RunArchive, SourceFile, Preset, RunPreset, RawRecord,
    Activity, Entity, SpatialExpression, SpatialComponent,
    TemporalExpression, TemporalComponent, Observation, SpatialIntegration, SpatialIndex,
    TimespanIntegration, Analysis, EditLog,
)


def _assert_descriptions_complete() -> None:
    """Fail loudly when the description map and the dataclasses disagree.

    The guard that lets an explicit map stay honest: every table needs a table-level row,
    every field needs its own, and nothing may describe a field that no longer exists.
    Called at import, so the failure lands at the edit rather than in a shipped database.
    """
    described = set(DESCRIPTIONS)
    expected: set[tuple[str, str | None]] = set()
    for model in _DESCRIBED_MODELS:
        expected.add((model.__name__, None))
        expected |= {(model.__name__, f.name) for f in dataclass_fields(model)}
    missing = expected - described
    extra = described - expected
    assert not missing, f"model fields with no description: {sorted(missing)}"
    assert not extra, f"descriptions for fields that do not exist: {sorted(extra)}"


_assert_descriptions_complete()

# --- Sentinels ------------------------------------------------------------
# Sentinel for an applied utc_offset when a naive timestamp was materialised by
# ASSUMING UTC: the instant is recorded, but the true offset is unknown. Distinct
# from None (not applicable) and from a real 0.0, so an examiner sees the assumption
# loudly (the ` N/A` on the datetime_iso).
# No local zone is configured, so the run cannot state the offset to local. Same loud
# "N/A" text as before, renamed with the column: it no longer marks an ASSUMED UTC clock
# (that fact now lives in ``timespan_integration.method`` as ``assume_utc``).
LOCAL_OFFSET_UNKNOWN = "N/A"


# --- Type & unit metadata (drives cast inference + unit conversion) -------
# A model field's type is a property of the model, declared once here, so a preset never
# has to restate `cast: float` / `cast: int` on a numeric mapping. ``column_cast`` is keyed
# on the ASSIGNABLE field name — what a preset writes in `to:` (presets/spec.py
# ASSIGNABLE_FIELDS) — because that is exactly the string its one caller
# (presets/resolve.py) passes. For the metric float fields the assignable name happens to
# equal the integration column; for the two ANGLE fields it does NOT (a preset maps the
# datum-agnostic ``latitude`` / ``longitude``, whereas the canonical L2 write columns are
# ``latitude_wgs84`` / ``longitude_wgs84``), and for the integer fields it does NOT either
# (the assignable names are ``time_accuracy_us_lower`` / ``_upper``,
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
# The integer-typed assignable fields a preset may map:
# the two per-bound source-stated timing uncertainties. Keyed on the assignable NAME (see the
# block comment above), so e.g. a source value 2.0 for ``multiplicity`` coerces to int 2.
_INT_COLUMNS: frozenset[str] = frozenset({
    "time_accuracy_us_lower", "time_accuracy_us_upper"})


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
