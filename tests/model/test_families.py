"""Unit tests for the redesigned six-family assertion model.

Defines:    tests for the genesis derivation, the canonical OUTPUT_COLUMNS, and
            the to_flat_row projection.
Used by:    pytest.
Depends on: model_atlas.model.families.
"""
from __future__ import annotations

from model_atlas.model.families import (
    OUTPUT_COLUMNS,
    Bindings,
    Derived,
    EntityPositionLink,
    EntityTimeLink,
    Genesis,
    Provenance,
    RecordState,
    RecordType,
    SourceTier,
    Spatial,
    SpatialTemporalLink,
    SpatioTemporalAssertion,
    Temporal,
    TemporalBound,
)


def test_output_columns_count_and_uniqueness() -> None:
    assert len(OUTPUT_COLUMNS) == 45
    assert len(set(OUTPUT_COLUMNS)) == 45  # no duplicate column names


def test_entity_time_link_genesis_for_every_member() -> None:
    expected = {
        EntityTimeLink.OBSERVED_AT: Genesis.PRESENCE,
        EntityTimeLink.EVENT_AT: Genesis.ACTION,
        EntityTimeLink.RECORDED_AT: Genesis.SYSTEM,
        EntityTimeLink.REPORTED_FOR: Genesis.REPORTED,
        EntityTimeLink.INTENDED_FOR: Genesis.PLANNED,
        EntityTimeLink.UNKNOWN: Genesis.UNKNOWN,
    }
    # Every member must resolve, so a future addition cannot silently lack a genesis.
    assert {link: link.genesis for link in EntityTimeLink} == expected


def test_to_flat_row_keys_match_output_columns_in_order() -> None:
    row = SpatioTemporalAssertion().to_flat_row()
    assert tuple(row.keys()) == OUTPUT_COLUMNS


def test_default_assertion_is_all_none() -> None:
    row = SpatioTemporalAssertion().to_flat_row()
    assert set(row.values()) == {None}


def test_enums_serialise_to_their_string_value() -> None:
    assertion = SpatioTemporalAssertion(
        bindings=Bindings(
            entity_position_link=EntityPositionLink.AT,
            entity_time_link=EntityTimeLink.OBSERVED_AT,
            spatial_temporal_link=SpatialTemporalLink.INSTANT,
        ),
        provenance=Provenance(
            source_tier=SourceTier.SECONDARY,
            deleted=RecordState.RECOVERED,
        ),
        derived=Derived(record_type=RecordType.MAIN, record_rank=1),
    )
    row = assertion.to_flat_row()
    assert row["entity_position_link"] == "at"
    assert row["entity_time_link"] == "observed_at"
    assert row["spatial_temporal_link"] == "instant"
    assert row["source_tier"] == "secondary"
    assert row["deleted"] == "recovered"
    assert row["record_type"] == "main"
    assert row["record_rank"] == 1


def test_temporal_bounds_expand_to_prefixed_columns() -> None:
    assertion = SpatioTemporalAssertion(
        temporal=Temporal(
            lower=TemporalBound(raw="694223890", source_field="FIRST_SEEN", unix_us=1672531200000000),
            upper=TemporalBound(raw="694223990", source_field="LAST_SEEN", unix_us=1672531300000000),
            time_zone="UTC+00:00",
            accuracy_us=500,
            temporal_source="internal_clock",
        ),
    )
    row = assertion.to_flat_row()
    assert row["time_lower_raw"] == "694223890"
    assert row["time_lower_source_field"] == "FIRST_SEEN"
    assert row["time_lower_unix_us"] == 1672531200000000
    assert row["time_upper_raw"] == "694223990"
    assert row["time_upper_source_field"] == "LAST_SEEN"
    assert row["time_upper_unix_us"] == 1672531300000000
    # time_zone is shared by both bounds (a single column).
    assert row["time_zone"] == "UTC+00:00"
    assert row["time_accuracy_us"] == 500
    assert row["temporal_source"] == "internal_clock"


def test_spatial_fields_including_heading_and_beam() -> None:
    assertion = SpatioTemporalAssertion(
        spatial=Spatial(
            latitude_wgs84=48.8566,
            longitude_wgs84=2.3522,
            horizontal_speed_kmh=36.0,
            heading_deg=270.0,
            heading_accuracy_deg=5.0,
            beam_azimuth_deg=120.0,
            beam_width_deg=65.0,
        ),
    )
    row = assertion.to_flat_row()
    assert row["latitude_wgs84"] == 48.8566
    assert row["longitude_wgs84"] == 2.3522
    assert row["horizontal_speed_kmh"] == 36.0
    assert row["heading_deg"] == 270.0
    assert row["heading_accuracy_deg"] == 5.0
    assert row["beam_azimuth_deg"] == 120.0
    assert row["beam_width_deg"] == 65.0
