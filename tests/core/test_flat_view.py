"""Tests for the flat-CSV projection (``model_atlas.core.flat_view``).

Defines:    the column-contract test (the projected frame carries exactly the
            docs/csv_migration.md columns, in order, against the worked-example demo
            DB), the one-row-per-observation grain test (28 rows, corner-less observations
            kept with empty cells), the unquoted-numeric rendering test, the derived
            ``locates`` test (single source of truth = the vocabulary enum), and the
            ``orig_<col>`` passthrough tests (first-appearance order, verbatim values,
            trailing-``_`` collision disambiguation).
Used by:    the verification gate (pytest tests/core/test_flat_view.py).
Depends on: ``model_atlas.core.flat_view`` (under test), ``model_atlas.core.vocabularies``
            (the ``EntityPositionLink.locates`` derivation the projection must agree
            with), pandas, and the worked-example demo DB
            ``docs/examples/matlas_example.sqlite`` (28 observations, one positionless
            nav-origin leg).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from model_atlas.core import flat_view
from model_atlas.core import vocabularies as V

_DEMO_DB = Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"

# The csv_migration.md contract transcribed LITERALLY (regeneration procedure: the
# unchanged/old block first for reviewer diffing, then the new columns; orig_ follows).
# Deliberately a second, independent declaration: a drive-by edit of the module's
# CANONICAL_COLUMNS cannot silently pass — the doc's list is the oracle, not the code's.
_EXPECTED_CANONICAL = (
    # -- old columns, mapped (csv_migration.md "Every old column, mapped" order) --
    "entity", "entity_type", "concerns",
    "time_lower_raw", "time_lower_source_field", "time_lower_unix_utc_us",
    "time_upper_raw", "time_upper_source_field", "time_upper_unix_utc_us",
    "utc_offset_hours_lower", "utc_offset_hours_upper",   # F2: the old single column, split
    "lower_accuracy_us", "upper_accuracy_us", "temporal_source",
    "latitude_wgs84", "latitude_source_field",
    "longitude_wgs84", "longitude_source_field",
    "altitude_m",
    "spatial_role",           # old position_type (F3 merge)
    "raw_position",
    "spatial_source",         # old position_source (the mechanism)
    "horizontal_accuracy_m", "vertical_accuracy_m",
    "horizontal_speed_kmh", "vertical_speed_kmh",
    "horizontal_speed_accuracy_kmh", "vertical_speed_accuracy_kmh",
    "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg", "beam_width_deg",
    "entity_position_link", "entity_time_link", "spatial_temporal_link",
    "raw_source_path", "input_file_path", "input_record_id", "source_record_uid",
    "row_uid", "preset_id", "preset_name", "source_label", "source_tier", "deleted",
    "record_type", "record_rank",
    # -- new columns (csv_migration.md "New columns" order) ------------------------
    "case_number", "content_uid", "claim_digest",
    "raw_identifier", "identifier_type", "entity_role", "concerns_origin",
    "spatial_referential", "locates", "spatial_method", "temporal_method",
    "time_lower_stated_offset", "time_upper_stated_offset",
    "time_lower_datetime_iso", "time_upper_datetime_iso",
    "time_lower_date", "time_upper_date", "time_lower_time", "time_upper_time",
    "recovery_state", "recovery_source", "data_source",
    "activity_id", "activity_kind", "counterpart", "facets", "context",
    "s2_cell_id",
)


@pytest.fixture(scope="module")
def demo() -> sqlite3.Connection:
    # mode=ro: the projection is a READ-ONLY consumer surface; opening the demo fixture
    # read-only proves no test can mutate it and matches the exporter's real posture.
    connection = sqlite3.connect(f"file:{_DEMO_DB}?mode=ro", uri=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def frame(demo: sqlite3.Connection) -> pd.DataFrame:
    return flat_view.build_flat_view(demo)


# --- the column contract -----------------------------------------------------

def test_canonical_columns_match_csv_migration_contract() -> None:
    assert flat_view.CANONICAL_COLUMNS == _EXPECTED_CANONICAL


def test_frame_carries_contract_columns_in_order_then_orig(frame: pd.DataFrame) -> None:
    canonical = tuple(frame.columns[:len(_EXPECTED_CANONICAL)])
    assert canonical == _EXPECTED_CANONICAL
    trailing = list(frame.columns[len(_EXPECTED_CANONICAL):])
    # Everything after the canonical block is the passthrough; nothing else may appear.
    assert trailing, "the demo DB has raw_json keys, so orig_ columns must be present"
    assert all(name.startswith(flat_view.PASSTHROUGH_PREFIX) for name in trailing)


def test_orig_columns_excluded_on_request(demo: sqlite3.Connection) -> None:
    bare = flat_view.build_flat_view(demo, include_source_columns=False)
    assert tuple(bare.columns) == _EXPECTED_CANONICAL


# --- the grain: one row per observation ----------------------------------------

def test_one_row_per_observation(demo: sqlite3.Connection, frame: pd.DataFrame) -> None:
    expected = {row[0] for row in demo.execute("SELECT observation_id FROM observation")}
    assert len(frame) == 28 == len(expected)
    # row_uid = observation_id (migration row: the founding-defect fix — genuinely unique).
    assert set(frame["row_uid"]) == expected
    assert frame["row_uid"].is_unique


def test_row_uid_content_uid_case_number(demo: sqlite3.Connection,
                                         frame: pd.DataFrame) -> None:
    hashes = dict(demo.execute("SELECT observation_id, content_hash FROM observation"))
    assert all(hashes[uid] == cu for uid, cu in zip(frame["row_uid"], frame["content_uid"]))
    assert set(frame["case_number"]) == {"GE-2026-04412"}


def test_positionless_observation_kept_with_empty_coordinates(frame: pd.DataFrame) -> None:
    # The nav-search ORIGIN leg ("Home") has a spatial expression but no integration —
    # the flat rule says the line survives with EMPTY coordinate cells, never dropped
    # and never invented (§2 "a positionless claim vanishes from CSV" guard).
    row = frame[(frame["preset_id"] == "nav_generic") & (frame["raw_position"] == "Home")]
    assert len(row) == 1
    assert row["latitude_wgs84"].isna().all()
    assert row["longitude_wgs84"].isna().all()
    assert row["s2_cell_id"].isna().all()
    assert row["spatial_method"].isna().all()
    # The expression header still projects: the place was EXPRESSED, just not integrated.
    assert row["spatial_referential"].tolist() == ["place_name"]


# --- numeric rendering --------------------------------------------------------

def test_numeric_columns_are_numeric_dtypes(frame: pd.DataFrame) -> None:
    assert pd.api.types.is_float_dtype(frame["latitude_wgs84"])
    assert pd.api.types.is_float_dtype(frame["beam_azimuth_deg"])
    # The INTEGER columns are nullable Int64 (exact — no float64 detour); the per-bound
    # accuracies are clean whole-µs integers, not the numeric-union TEXT the offsets are.
    assert str(frame["time_lower_unix_utc_us"].dtype) == "Int64"
    assert str(frame["lower_accuracy_us"].dtype) == "Int64"
    assert str(frame["upper_accuracy_us"].dtype) == "Int64"
    assert str(frame["s2_cell_id"].dtype) == "Int64"
    assert pd.api.types.is_numeric_dtype(frame["record_rank"])
    # Some applied offsets carry the "N/A" assumed-UTC sentinel in the demo, so the column
    # is a MIX (object dtype): the sentinel stays text (quoted) — "value, zone unknown" must
    # never render as a bare number — while genuine offsets are restored to numbers per value.
    assert not pd.api.types.is_numeric_dtype(frame["utc_offset_hours_lower"])
    offset_values = set(frame["utc_offset_hours_lower"].dropna())
    assert "N/A" in offset_values                                    # the sentinel, kept text
    assert any(isinstance(value, float) for value in offset_values)  # real offsets, numeric


def test_s2_cell_id_exact_int64(demo: sqlite3.Connection, frame: pd.DataFrame) -> None:
    # The S2 leaf cell exceeds float64's 53-bit integer range; any float detour would
    # silently corrupt it. Compare every projected value to the stored int, exactly.
    stored = dict(demo.execute(
        "SELECT a.observation_id, six.s2_cell_id FROM observation a "
        "LEFT JOIN spatial_integration si "
        "  ON si.spatial_expression_id = a.spatial_expression_id "
        "LEFT JOIN spatial_index six "
        "  ON six.spatial_integration_id = si.spatial_integration_id"))
    for uid, value in zip(frame["row_uid"], frame["s2_cell_id"]):
        if pd.isna(value):
            assert stored[uid] is None
        else:
            assert int(value) == stored[uid]


def test_numeric_cells_render_unquoted(frame: pd.DataFrame) -> None:
    # Render exactly as the exporter does (QUOTE_NONNUMERIC): numeric dtypes come out
    # bare, text comes out quoted — so the sentinel stays visibly a string.
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, sep=";", quoting=csv.QUOTE_NONNUMERIC)
    rendered = buffer.getvalue()
    assert ";46.205;" in rendered          # CELL_A latitude, unquoted float
    assert ';"N/A";' in rendered           # assumed-UTC applied offset, quoted text
    assert '"46.205"' not in rendered


# --- derived locates ----------------------------------------------------------

def test_locates_derived_from_the_vocabulary(frame: pd.DataFrame) -> None:
    # Single source of truth (decision 11): each row's locates must equal the enum's own
    # derivation of its entity_position_link — no second mapping may exist to disagree.
    for link, locates in zip(frame["entity_position_link"], frame["locates"]):
        assert locates == V.EntityPositionLink(link).locates.value
    # The demo exercises self / infrastructure / referenced.
    assert {"self", "infrastructure", "referenced"} <= set(frame["locates"])


# --- the orig_ passthrough ------------------------------------------------------

def test_orig_columns_union_in_first_appearance_order(demo: sqlite3.Connection,
                                                      frame: pd.DataFrame) -> None:
    # Recompute the expected order straight from raw_record in rowid (ingest) order:
    # the union of every record's keys, first appearance wins (determinism rule 1).
    seen: set[str] = set()
    expected: list[str] = []
    for (raw_json,) in demo.execute("SELECT raw_json FROM raw_record ORDER BY rowid"):
        for key in json.loads(raw_json):
            if key not in seen:
                seen.add(key)
                expected.append(f"{flat_view.PASSTHROUGH_PREFIX}{key}")
    assert list(frame.columns[len(_EXPECTED_CANONICAL):]) == expected


def test_orig_values_are_verbatim(demo: sqlite3.Connection, frame: pd.DataFrame) -> None:
    # Every row repeats ITS OWN record's verbatim values; keys the record lacks are blank.
    raw = dict(demo.execute("SELECT raw_record_id, raw_json FROM raw_record"))
    a_party = frame[frame["source_label"] == "CDR op-A"].iloc[0]
    record = json.loads(raw[a_party["source_record_uid"]])
    assert a_party["orig_a_party"] == record["a_party"] == "+41 79 123 45 67"
    assert pd.isna(a_party["orig_ZLATITUDE"])   # a CDR record has no iOS-cache key


def test_passthrough_collision_gets_trailing_underscore() -> None:
    # A source field whose prefixed name collides with a canonical column must be
    # disambiguated (trailing "_"), never allowed to shadow the canonical column.
    names = flat_view._passthrough_names(["position", "position_"], "raw_")
    assert names["position"] == "raw_position_"       # "raw_position" is canonical
    assert names["position_"] == "raw_position__"     # and chains must not collide either


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ('a"b', "double-quote in the key"),          # the double-quote case
        (r"a\b", "backslash in the key"),            # the backslash case
        (r'a"\b', "both a quote and a backslash"),   # both together
        ("plain", "an ordinary key"),                # the control
    ],
)
def test_orig_json_path_extracts_exotic_field_verbatim(field: str, value: str) -> None:
    # The orig_ passthrough builds a SQLite JSON path from each raw key. A key holding a
    # double-quote or a backslash must still resolve to its verbatim value; the pre-fix
    # SQL-literal doubling ("->"") left json_extract returning NULL for every row (silent
    # verbatim-data loss). Exercise the exact production path builder against real sqlite.
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE raw_record (raw_json TEXT)")
    connection.execute(
        "INSERT INTO raw_record VALUES (?)", (json.dumps({field: value}),))
    path = flat_view._json_path_literal(field)
    (extracted,) = connection.execute(
        f"SELECT json_extract(raw_json, {path}) FROM raw_record").fetchone()
    assert extracted == value


def test_restore_numeric_dtype_is_per_value() -> None:
    # A numeric-union TEXT column mixing real offsets with the "N/A" sentinel must restore
    # the genuine numbers to numeric values (rendered unquoted) while the sentinel stays
    # text (quoted) — not drag the whole column to TEXT because one sibling row is "N/A".
    frame = pd.DataFrame({"utc_offset_hours_lower": ["1.0", "N/A", "0.0", None]})
    flat_view._restore_numeric_dtype(frame, "utc_offset_hours_lower")
    restored = list(frame["utc_offset_hours_lower"])
    assert restored[0] == 1.0 and isinstance(restored[0], float)   # real offset -> number
    assert restored[1] == "N/A"                                    # sentinel -> kept text
    assert restored[2] == 0.0 and isinstance(restored[2], float)   # real offset -> number
    assert restored[3] is None                                     # empty -> stays empty
    # And under the exporter's QUOTE_NONNUMERIC the numbers come out bare, the sentinel quoted.
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, sep=";", quoting=csv.QUOTE_NONNUMERIC)
    rendered = buffer.getvalue().splitlines()
    # (an empty cell renders as a quoted "" here, exactly as an all-null offset column does.)
    assert rendered == ['"utc_offset_hours_lower"', "1.0", '"N/A"', "0.0", '""']
