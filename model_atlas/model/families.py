"""Typed object model for one spatio-temporal assertion (six column families).

Defines:    the controlled-vocabulary enums for the three edges and the record/
            source state, the six column-family dataclasses (Entity, Temporal,
            Spatial, Bindings, Provenance, Derived), the composing
            SpatioTemporalAssertion, the canonical OUTPUT_COLUMNS order, and the
            flat-row projection used for CSV export.
Used by:    transforms (build these from preset mappings), untangle (ranks them),
            export (flattens them to CSV rows via OUTPUT_COLUMNS).
Depends on: standard library only (dataclasses, enum, uuid is not used here).

This is the canonical 45-column model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --- Controlled vocabularies ----------------------------------------------
# Each edge value is a typed variant rather than a free string, so an invalid
# mode is caught when a preset is parsed instead of being written to the CSV.

class Genesis(str, Enum):
    """Derived view of the entity-time edge: how the trace came to exist."""

    PRESENCE = "presence"
    ACTION = "action"
    SYSTEM = "system"
    REPORTED = "reported"
    PLANNED = "planned"
    UNKNOWN = "unknown"


class EntityTimeLink(str, Enum):
    """Entity-time (E-T) edge: what the clock measured about the entity."""

    OBSERVED_AT = "observed_at"    # a sensor registered the entity at this time
    EVENT_AT = "event_at"          # the entity caused an event at this time
    RECORDED_AT = "recorded_at"    # the system wrote the row at this time (may lag)
    REPORTED_FOR = "reported_for"  # a recalled/declared time, not measured
    INTENDED_FOR = "intended_for"  # a future/planned time
    UNKNOWN = "unknown"

    @property
    def genesis(self) -> Genesis:
        # Genesis is never stored: it is a deterministic function of the E-T
        # edge, so it can never contradict the edge it derives from.
        return _ETLINK_GENESIS[self]


class EntityPositionLink(str, Enum):
    """Entity-position (E-S) edge: whose location this is / how it is bound."""

    AT = "at"                                # the entity's own measured position
    WITHIN_RANGE_OF = "within_range_of"      # inside a covering object's range
    AT_FIXED_DETECTOR = "at_fixed_detector"  # passed a fixed sensor
    REFERENCES = "references"                # pointed at the place, not present
    CLAIMED_AT = "claimed_at"                # declared by testimony/message
    INFERRED = "inferred"                    # analytically derived
    UNKNOWN = "unknown"


class SpatialTemporalLink(str, Enum):
    """Spatial-temporal (S-T) edge: how the position distributes over the interval.

    Absorbs the former ``Temporal relation`` field — they are one edge.
    """

    INSTANT = "instant"
    CONTINUOUS_DURING_INTERVAL = "continuous_during_interval"
    ONCE_DURING_INTERVAL = "once_during_interval"
    SPORADIC_DURING_INTERVAL = "sporadic_during_interval"
    NEVER_DURING_INTERVAL = "never_during_interval"  # an exclusion / alibi
    UNKNOWN_DURING_INTERVAL = "unknown_during_interval"


class RecordType(str, Enum):
    """Derived rank label assigned by untangle within a group."""

    MAIN = "main"
    ADDITIONAL = "additional"


class SourceTier(str, Enum):
    """Evidential tier: the original artefact, or a tool's output.

    Primary is first-hand (the acquisition was parsed); secondary is second-hand
    (a tool's export was ingested and its interpretation inherited).
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    UNKNOWN = "unknown"


class RecordState(str, Enum):
    """Source record recovery state. ``recovered`` (e.g. from WAL/freelist) is
    deliberately distinct from ``deleted`` because their evidential weight differs.
    """

    INTACT = "intact"
    DELETED = "deleted"
    RECOVERED = "recovered"
    UNKNOWN = "unknown"


_ETLINK_GENESIS: dict[EntityTimeLink, Genesis] = {
    EntityTimeLink.OBSERVED_AT: Genesis.PRESENCE,
    EntityTimeLink.EVENT_AT: Genesis.ACTION,
    EntityTimeLink.RECORDED_AT: Genesis.SYSTEM,
    EntityTimeLink.REPORTED_FOR: Genesis.REPORTED,
    EntityTimeLink.INTENDED_FOR: Genesis.PLANNED,
    EntityTimeLink.UNKNOWN: Genesis.UNKNOWN,
}


# --- The six column families ----------------------------------------------

@dataclass(frozen=True)
class Entity:
    """Who/what the assertion is about (3 columns)."""

    entity: str | None = None
    entity_type: str | None = None
    linked_entity: str | None = None  # the real-world actor, e.g. car -> owner


@dataclass(frozen=True)
class TemporalBound:
    """One end of the interval: the raw/normalised pair plus its source field.

    ``raw`` and ``unix_us`` are the source-original and exploitable forms of the
    same instant; ``source_field`` records which source column it came from.
    """

    raw: Any | None = None
    source_field: str | None = None
    unix_us: int | None = None


@dataclass(frozen=True)
class Temporal:
    """When — an explicit interval, never a single timestamp (9 columns)."""

    lower: TemporalBound = field(default_factory=TemporalBound)
    upper: TemporalBound = field(default_factory=TemporalBound)
    time_zone: str | None = None        # shared by both bounds, e.g. "UTC+00:00"
    accuracy_us: int | float | None = None
    temporal_source: str | None = None  # mechanism: NTP, internal_clock, ...


@dataclass(frozen=True)
class Spatial:
    """Where, plus metric quality, movement and beam geometry (18 columns).

    Units are named on each field: ``_m`` metres, ``_kmh`` km/h, ``_deg`` degrees;
    coordinates are WGS84. ``altitude_m`` keeps the source's own datum (iOS = MSL,
    Android = ellipsoid), recorded in the preset, not assumed. ``heading_*`` is the
    entity's course over ground; ``beam_*`` is a fixed sensor's coverage direction.
    """

    latitude_wgs84: float | None = None
    latitude_source_field: str | None = None    # source column latitude was read from
    longitude_wgs84: float | None = None
    longitude_source_field: str | None = None   # source column longitude was read from
    altitude_m: float | None = None
    position_type: str | None = None
    raw_position: Any | None = None      # source-original, e.g. "I'm at home"
    position_source: str | None = None   # mechanism: GNSS, WiFi, cell, ...
    horizontal_accuracy_m: float | None = None
    vertical_accuracy_m: float | None = None
    horizontal_speed_kmh: float | None = None
    vertical_speed_kmh: float | None = None
    horizontal_speed_accuracy_kmh: float | None = None
    vertical_speed_accuracy_kmh: float | None = None
    heading_deg: float | None = None           # course over ground, 0..360 from north
    heading_accuracy_deg: float | None = None  # heading uncertainty
    beam_azimuth_deg: float | None = None       # coverage bearing for sectored sources
    beam_width_deg: float | None = None         # angular spread; null/360 = omni


@dataclass(frozen=True)
class Bindings:
    """The three edges of the entity/space/time triangle (3 columns)."""

    entity_position_link: EntityPositionLink | None = None
    entity_time_link: EntityTimeLink | None = None
    spatial_temporal_link: SpatialTemporalLink | None = None


@dataclass(frozen=True)
class Provenance:
    """Where the trace came from, which record it is, how it was read, and its state
    (9 columns).

    Four questions in order: *where from* (``raw_source_path`` → ``input_file_path`` →
    ``input_record_id``), *which record + its keys* (``input_record_id`` for a human — the
    1-based source line number by default, or a mapped tool locator; ``source_record_uid``
    as a deterministic per-source-record machine key shared by every output row derived from
    one source record; ``row_uid`` as the unique per-output-row key), *how read* (``preset_id``
    / ``preset_name`` / ``source_label`` / ``source_tier``), and *record state* (``deleted``).
    """

    raw_source_path: str | None = None    # where the trace came from (preset-mapped: device path / tool column)
    input_file_path: str | None = None    # full path of the outermost on-disk artifact matlas opened
    input_record_id: str | None = None    # which record in that file: "<table-or-sheet>#<line>" (1-based) or a tool locator
    source_record_uid: str | None = None  # per-source-record UID; shared by every output row of one source record
    row_uid: str | None = None            # unique per OUTPUT row (differentiates every emitted row)
    preset_id: str | None = None          # stable machine key of the applied preset
    preset_name: str | None = None        # human title of the applied preset
    source_label: str | None = None       # tool label (secondary) or any descriptive label (primary)
    source_tier: SourceTier | None = None
    deleted: RecordState | None = None


@dataclass(frozen=True)
class Derived:
    """Untangle output, recomputed across assertions (2 columns)."""

    record_type: RecordType | None = None
    record_rank: int | None = None


# Canonical output order. The flat CSV and every consumer read this single tuple,
# so the column set and order are defined in exactly one place.
OUTPUT_COLUMNS: tuple[str, ...] = (
    # entity
    "entity",
    "entity_type",
    "linked_entity",
    # temporal
    "time_lower_raw",
    "time_lower_source_field",
    "time_lower_unix_us",
    "time_upper_raw",
    "time_upper_source_field",
    "time_upper_unix_us",
    "time_zone",
    "time_accuracy_us",
    "temporal_source",
    # spatial
    "latitude_wgs84",
    "latitude_source_field",
    "longitude_wgs84",
    "longitude_source_field",
    "altitude_m",
    "position_type",
    "raw_position",
    "position_source",
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
    # bindings
    "entity_position_link",
    "entity_time_link",
    "spatial_temporal_link",
    # provenance
    "raw_source_path",
    "input_file_path",
    "input_record_id",
    "source_record_uid",
    "row_uid",
    "preset_id",
    "preset_name",
    "source_label",
    "source_tier",
    "deleted",
    # derived
    "record_type",
    "record_rank",
)


# --- Type & unit metadata (drives v3 cast inference + unit conversion) ------
# A model column's type is a property of the model, declared once here, so a preset
# never has to restate `cast: float` on every numeric mapping.

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

    Used by the assembly engine to apply an implicit cast so presets omit `cast:`.
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


def _value(item: Any) -> Any:
    # Enums serialise to their string value; everything else (including None)
    # passes through unchanged.
    return item.value if isinstance(item, Enum) else item


@dataclass(frozen=True)
class SpatioTemporalAssertion:
    """One trace = one row: the reified ternary fact plus its wrapper layers."""

    entity: Entity = field(default_factory=Entity)
    temporal: Temporal = field(default_factory=Temporal)
    spatial: Spatial = field(default_factory=Spatial)
    bindings: Bindings = field(default_factory=Bindings)
    provenance: Provenance = field(default_factory=Provenance)
    derived: Derived = field(default_factory=Derived)
    # Unmapped source columns kept verbatim (the E\\M frontier + residue).
    # mutable default — shared across instances if = {} were used.
    details_extra: dict[str, Any] = field(default_factory=dict)

    def to_flat_row(self) -> dict[str, Any]:
        """Project the assertion onto the canonical columns, in order.

        ``details_extra`` is intentionally left off the flat row; it is written
        separately by the details transform.
        """
        flat: dict[str, Any] = {
            "entity": self.entity.entity,
            "entity_type": self.entity.entity_type,
            "linked_entity": self.entity.linked_entity,
            "time_lower_raw": self.temporal.lower.raw,
            "time_lower_source_field": self.temporal.lower.source_field,
            "time_lower_unix_us": self.temporal.lower.unix_us,
            "time_upper_raw": self.temporal.upper.raw,
            "time_upper_source_field": self.temporal.upper.source_field,
            "time_upper_unix_us": self.temporal.upper.unix_us,
            "time_zone": self.temporal.time_zone,
            "time_accuracy_us": self.temporal.accuracy_us,
            "temporal_source": self.temporal.temporal_source,
            "latitude_wgs84": self.spatial.latitude_wgs84,
            "latitude_source_field": self.spatial.latitude_source_field,
            "longitude_wgs84": self.spatial.longitude_wgs84,
            "longitude_source_field": self.spatial.longitude_source_field,
            "altitude_m": self.spatial.altitude_m,
            "position_type": self.spatial.position_type,
            "raw_position": self.spatial.raw_position,
            "position_source": self.spatial.position_source,
            "horizontal_accuracy_m": self.spatial.horizontal_accuracy_m,
            "vertical_accuracy_m": self.spatial.vertical_accuracy_m,
            "horizontal_speed_kmh": self.spatial.horizontal_speed_kmh,
            "vertical_speed_kmh": self.spatial.vertical_speed_kmh,
            "horizontal_speed_accuracy_kmh": self.spatial.horizontal_speed_accuracy_kmh,
            "vertical_speed_accuracy_kmh": self.spatial.vertical_speed_accuracy_kmh,
            "heading_deg": self.spatial.heading_deg,
            "heading_accuracy_deg": self.spatial.heading_accuracy_deg,
            "beam_azimuth_deg": self.spatial.beam_azimuth_deg,
            "beam_width_deg": self.spatial.beam_width_deg,
            "entity_position_link": _value(self.bindings.entity_position_link),
            "entity_time_link": _value(self.bindings.entity_time_link),
            "spatial_temporal_link": _value(self.bindings.spatial_temporal_link),
            "raw_source_path": self.provenance.raw_source_path,
            "input_file_path": self.provenance.input_file_path,
            "input_record_id": self.provenance.input_record_id,
            "source_record_uid": self.provenance.source_record_uid,
            "row_uid": self.provenance.row_uid,
            "preset_id": self.provenance.preset_id,
            "preset_name": self.provenance.preset_name,
            "source_label": self.provenance.source_label,
            "source_tier": _value(self.provenance.source_tier),
            "deleted": _value(self.provenance.deleted),
            "record_type": _value(self.derived.record_type),
            "record_rank": self.derived.record_rank,
        }
        # Re-project through OUTPUT_COLUMNS so the row's column set and order are
        # guaranteed to match the canonical schema; a missing key fails loudly here
        # rather than producing a silently malformed CSV.
        return {column: flat[column] for column in OUTPUT_COLUMNS}
