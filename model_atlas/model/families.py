"""Typed object model for one spatio-temporal assertion (six column families).

Defines:    the controlled-vocabulary enums for the three edges and the record/
            source state, the six column-family dataclasses (Entity, Temporal,
            Spatial, Bindings, Provenance, Derived), the composing
            SpatioTemporalAssertion, the canonical OUTPUT_COLUMNS order, and the
            flat-row projection used for CSV export.
Used by:    transforms (build these from preset mappings), untangle (ranks them),
            export (flattens them to CSV rows via OUTPUT_COLUMNS).
Depends on: standard library only (dataclasses, enum, uuid is not used here).

This is the canonical 41-column model.
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

    ``raw`` and ``unix_ns`` are the source-original and exploitable forms of the
    same instant; ``source_field`` records which source column it came from.
    """

    raw: Any | None = None
    source_field: str | None = None
    unix_ns: int | None = None


@dataclass(frozen=True)
class Temporal:
    """When — an explicit interval, never a single timestamp (9 columns)."""

    lower: TemporalBound = field(default_factory=TemporalBound)
    upper: TemporalBound = field(default_factory=TemporalBound)
    time_zone: str | None = None        # shared by both bounds, e.g. "UTC+00:00"
    accuracy_ns: int | float | None = None
    temporal_source: str | None = None  # mechanism: NTP, internal_clock, ...


@dataclass(frozen=True)
class Spatial:
    """Where, plus metric quality, movement and beam geometry (16 columns).

    Units are named on each field: ``_m`` metres, ``_kmh`` km/h, ``_deg`` degrees;
    coordinates are WGS84. ``altitude_m`` keeps the source's own datum (iOS = MSL,
    Android = ellipsoid), recorded in the preset, not assumed. ``heading_*`` is the
    entity's course over ground; ``beam_*`` is a fixed sensor's coverage direction.
    """

    latitude_wgs84: float | None = None
    longitude_wgs84: float | None = None
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
    """Chain of custody, sibling link and recovery state (8 columns)."""

    acquisition_path: str | None = None   # the archive/image
    source_file_path: str | None = None   # original file inside the device
    tool_label: str | None = None         # upstream parser/artefact label
    input_file: str | None = None         # what matlas actually ingested
    record_locator: str | None = None     # address back to the row (table+offset)
    source_tier: SourceTier | None = None
    source_row_id: str | None = None      # shared by every assertion from one row
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
    "time_lower_unix_ns",
    "time_upper_raw",
    "time_upper_source_field",
    "time_upper_unix_ns",
    "time_zone",
    "time_accuracy_ns",
    "temporal_source",
    # spatial
    "latitude_wgs84",
    "longitude_wgs84",
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
    "acquisition_path",
    "source_file_path",
    "tool_label",
    "input_file",
    "record_locator",
    "source_tier",
    "source_row_id",
    "deleted",
    # derived
    "record_type",
    "record_rank",
)


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
        """Project the assertion onto the 40 canonical columns, in order.

        ``details_extra`` is intentionally left off the flat row; it is written
        separately by the details transform.
        """
        flat: dict[str, Any] = {
            "entity": self.entity.entity,
            "entity_type": self.entity.entity_type,
            "linked_entity": self.entity.linked_entity,
            "time_lower_raw": self.temporal.lower.raw,
            "time_lower_source_field": self.temporal.lower.source_field,
            "time_lower_unix_ns": self.temporal.lower.unix_ns,
            "time_upper_raw": self.temporal.upper.raw,
            "time_upper_source_field": self.temporal.upper.source_field,
            "time_upper_unix_ns": self.temporal.upper.unix_ns,
            "time_zone": self.temporal.time_zone,
            "time_accuracy_ns": self.temporal.accuracy_ns,
            "temporal_source": self.temporal.temporal_source,
            "latitude_wgs84": self.spatial.latitude_wgs84,
            "longitude_wgs84": self.spatial.longitude_wgs84,
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
            "acquisition_path": self.provenance.acquisition_path,
            "source_file_path": self.provenance.source_file_path,
            "tool_label": self.provenance.tool_label,
            "input_file": self.provenance.input_file,
            "record_locator": self.provenance.record_locator,
            "source_tier": _value(self.provenance.source_tier),
            "source_row_id": self.provenance.source_row_id,
            "deleted": _value(self.provenance.deleted),
            "record_type": _value(self.derived.record_type),
            "record_rank": self.derived.record_rank,
        }
        # Re-project through OUTPUT_COLUMNS so the row's column set and order are
        # guaranteed to match the canonical schema; a missing key fails loudly here
        # rather than producing a silently malformed CSV.
        return {column: flat[column] for column in OUTPUT_COLUMNS}
