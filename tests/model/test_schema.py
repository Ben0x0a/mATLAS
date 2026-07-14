"""Unit tests for the SQLite schema and the assertion_flat view.

Defines:    a test that builds the schema in ``:memory:``, inserts one hand-built row set
            across every table, selects from ``assertion_flat`` and asserts the projected
            columns are exactly OUTPUT_COLUMNS (in order) and the values round-trip; plus a
            check that a bad enum value is rejected by a CHECK constraint.
Used by:    pytest.
Depends on: model_atlas.model.schema, model_atlas.model.families.
"""
from __future__ import annotations

import sqlite3

import pytest

from model_atlas.model.families import OUTPUT_COLUMNS
from model_atlas.model.schema import create_schema, entity_id_for


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    return conn


def test_assertion_flat_projects_output_columns_and_round_trips() -> None:
    conn = _fresh()
    row_uid = "row-1"
    entity_id = entity_id_for("device", "device")
    linked_id = entity_id_for("subject", None)

    conn.execute(
        "INSERT INTO source (source_id, input_file_path, preset_id, preset_name) "
        "VALUES ('s1', '/case/Cache.sqlite', 'preset.id', 'Preset Name')"
    )
    conn.execute(
        "INSERT INTO record (source_record_uid, source_id, line_no, input_record_id, raw_json) "
        "VALUES ('rec-1', 's1', 1, 'ZTABLE#1', '{}')"
    )
    conn.execute("INSERT INTO entity (entity_id, label, entity_type) VALUES (?, 'device', 'device')", (entity_id,))
    conn.execute("INSERT INTO entity (entity_id, label, entity_type) VALUES (?, 'subject', NULL)", (linked_id,))
    conn.execute(
        "INSERT INTO position (position_id, latitude_wgs84, latitude_source_field, "
        "longitude_wgs84, longitude_source_field, position_source, horizontal_accuracy_m) "
        "VALUES (?, 46.5197, 'Latitude', 6.6323, 'Longitude', 'GNSS', 12.0)",
        (row_uid,),
    )
    conn.execute(
        "INSERT INTO timespan (timespan_id, lower_raw, lower_source_field, lower_unix_utc_us, "
        "upper_raw, upper_source_field, upper_unix_utc_us, utc_offset_hours) "
        "VALUES (?, '06.12.2025 13:00', 'TS', 1765026000000000, '06.12.2025 13:00', 'TS', "
        "1765026000000000, '0.0')",
        (row_uid,),
    )
    conn.execute(
        "INSERT INTO assertion (row_uid, source_record_uid, entity_id, linked_entity_id, "
        "entity_position_link, entity_time_link, spatial_temporal_link, attribution_origin, "
        "raw_source_path, source_label, source_tier, deleted, record_type, record_rank) "
        "VALUES (?, 'rec-1', ?, ?, 'at', 'observed_at', 'instant', 'preset_mapped', "
        "'/case/Cache.sqlite', 'ZTABLE', 'primary', NULL, 'main', 1)",
        (row_uid, entity_id, linked_id),
    )
    conn.commit()

    cursor = conn.execute("SELECT * FROM assertion_flat")
    columns = [description[0] for description in cursor.description]
    assert columns == list(OUTPUT_COLUMNS)

    row = dict(zip(columns, cursor.fetchone()))
    # Values round-trip from every corner table, correctly aliased by the view.
    assert row["entity"] == "device"
    assert row["entity_type"] == "device"
    assert row["linked_entity"] == "subject"
    assert row["latitude_wgs84"] == 46.5197
    assert row["longitude_source_field"] == "Longitude"
    assert row["time_lower_unix_utc_us"] == 1765026000000000
    assert row["utc_offset_hours"] == "0.0"
    assert row["entity_position_link"] == "at"
    assert row["spatial_temporal_link"] == "instant"
    assert row["raw_source_path"] == "/case/Cache.sqlite"
    assert row["input_file_path"] == "/case/Cache.sqlite"
    assert row["input_record_id"] == "ZTABLE#1"
    assert row["source_record_uid"] == "rec-1"
    assert row["row_uid"] == "row-1"
    assert row["preset_id"] == "preset.id"
    assert row["source_label"] == "ZTABLE"
    assert row["source_tier"] == "primary"
    assert row["deleted"] is None
    assert row["record_type"] == "main"
    assert row["record_rank"] == 1
    # attribution_origin is stored but MUST NOT appear in the flat view (CSV parity).
    assert "attribution_origin" not in columns


def test_positionless_assertion_yields_null_spatial_columns() -> None:
    # An assertion with no position row: the LEFT JOIN leaves every spatial column NULL.
    conn = _fresh()
    conn.execute("INSERT INTO entity (entity_id, label, entity_type) VALUES ('e', 'device', 'device')")
    conn.execute(
        "INSERT INTO assertion (row_uid, entity_id, entity_time_link) VALUES ('r', 'e', 'observed_at')"
    )
    conn.commit()
    row = dict(
        zip(
            [d[0] for d in conn.execute("SELECT * FROM assertion_flat").description],
            conn.execute("SELECT * FROM assertion_flat").fetchone(),
        )
    )
    assert row["latitude_wgs84"] is None
    assert row["entity"] == "device"


def test_enum_check_rejects_invalid_link_value() -> None:
    conn = _fresh()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO assertion (row_uid, entity_position_link) VALUES ('r', 'not_a_link')"
        )
