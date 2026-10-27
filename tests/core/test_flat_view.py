"""Tests for the flat-CSV projection (``model_atlas.core.flat_view``).

Defines:    the column-contract test (the projected frame carries exactly the
            docs/csv_migration.md columns, in order, against the worked-example demo
            DB), the one-row-per-observation grain test (28 rows, corner-less observations
            kept with empty cells), the unquoted-numeric rendering test, and the
            ``orig_<col>`` passthrough tests (first-appearance order, verbatim values,
            trailing-``_`` collision disambiguation).
Used by:    the verification gate (pytest tests/core/test_flat_view.py).
Depends on: ``model_atlas.core.flat_view`` (under test), ``model_atlas.core.vocabularies``,
            pandas, and the worked-example demo DB
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
    # -- trace values and their obvious derivations: NO suffix -------------------
    # A value converted from a source datum is still that datum: `latitude_wgs84` is the
    # source's own coordinate in a named CRS, `entity` its own identifier normalised.
    "entity", "entity_type",
    "time_lower_raw", "time_lower_source_field", "time_lower_unix_utc_us",
    "time_upper_raw", "time_upper_source_field", "time_upper_unix_utc_us",
    "offset_to_local_hours_lower", "offset_to_local_hours_upper",
    "lower_accuracy_us", "upper_accuracy_us", "temporal_source",
    "latitude_wgs84", "latitude_source_field",
    "longitude_wgs84", "longitude_source_field", "altitude_m",
    "position_raw", "spatial_source",
    "horizontal_accuracy_m", "vertical_accuracy_m",
    "horizontal_speed_kmh", "vertical_speed_kmh",
    "horizontal_speed_accuracy_kmh", "vertical_speed_accuracy_kmh",
    "heading_deg", "heading_accuracy_deg", "beam_azimuth_deg", "beam_width_deg",
    # -- the three stored links (decision 55): preset-stated, so suffixed --------
    "entity_link_matlas", "spatial_link_matlas", "temporal_link_matlas",
    # -- acquisition paths come from the trace; the uid does not ----------------
    "raw_source_path", "input_file_path", "input_record_id", "source_record_uid_matlas",
    # -- mATLAS processing metadata ---------------------------------------------
    "row_uid_matlas", "preset_id_matlas", "preset_name_matlas",
    "source_label_matlas", "source_tier_matlas",
    "record_rank_matlas", "record_order_matlas",  # record_order derived from rank == 1
    "case_number_matlas", "observation_content_uid_matlas", "spatiotemporal_entity_hash_matlas",
    "raw_identifier", "identifier_type",
    # -- the position bearer: a real entity from the trace ----------------------
    "bearer_entity", "bearer_raw_identifier",
    "bearer_identifier_type", "bearer_entity_type",
    # -- integration projections -------------------------------------------------
    "recovery_state_matlas", "recovery_source_matlas",
    "reported_data_source", "s2_cell_id_matlas",
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
    assert set(frame["row_uid_matlas"]) == expected
    assert frame["row_uid_matlas"].is_unique


def test_row_uid_observation_content_uid_case_number(demo: sqlite3.Connection,
                                         frame: pd.DataFrame) -> None:
    hashes = dict(demo.execute("SELECT observation_id, observation_content_hash FROM observation"))
    assert all(hashes[uid] == cu for uid, cu in zip(frame["row_uid_matlas"], frame["observation_content_uid_matlas"]))
    assert set(frame["case_number_matlas"]) == {"GE-2026-04412"}


def test_positionless_observation_kept_with_empty_coordinates(frame: pd.DataFrame) -> None:
    # The nav-search ORIGIN leg ("Home") has a spatial expression but no integration —
    # the flat rule says the line survives with EMPTY coordinate cells, never dropped
    # and never invented (§2 "a positionless claim vanishes from CSV" guard).
    row = frame[(frame["preset_id_matlas"] == "nav_generic")
                & frame["latitude_wgs84"].isna()]
    assert len(row) == 2   # both nav legs: neither address nor place name was integrated
    assert row["latitude_wgs84"].isna().all()
    assert row["longitude_wgs84"].isna().all()
    assert row["s2_cell_id_matlas"].isna().all()
    # The place was EXPRESSED, just not integrated: the verbatim raw still projects, which
    # is the whole point of keeping the line. (`spatial_referential` and `spatial_method`
    # left the projection — the referential is already named in the integration column
    # titles, and the method lives in the run DB.)
    # position_raw records only what produced the coordinates, and nothing did here, so it
    # is empty too. The place survives at L1 in spatial_component, one join away.
    assert row["position_raw"].isna().all()


# --- numeric rendering --------------------------------------------------------

def test_numeric_columns_are_numeric_dtypes(frame: pd.DataFrame) -> None:
    assert pd.api.types.is_float_dtype(frame["latitude_wgs84"])
    assert pd.api.types.is_float_dtype(frame["beam_azimuth_deg"])
    # The INTEGER columns are nullable Int64 (exact — no float64 detour); the per-bound
    # accuracies are clean whole-µs integers, not the numeric-union TEXT the offsets are.
    assert str(frame["time_lower_unix_utc_us"].dtype) == "Int64"
    assert str(frame["lower_accuracy_us"].dtype) == "Int64"
    assert str(frame["upper_accuracy_us"].dtype) == "Int64"
    assert str(frame["s2_cell_id_matlas"].dtype) == "Int64"
    assert pd.api.types.is_numeric_dtype(frame["record_rank_matlas"])
    # The offset to local is stored TEXT (its model type is a float|str union, so a run
    # with no configured zone can hold the "N/A" sentinel), and is restored to numeric per
    # value. The demo DOES configure a zone, so every row here carries a real offset and the
    # whole column comes back numeric — the sentinel is a RUN-level state now, not a
    # per-row one, which is exactly why no demo row can exhibit it. The mixed case (some
    # numbers, some sentinel) is covered directly by the _restore_numeric_dtype unit test.
    offset_values = set(frame["offset_to_local_hours_lower"].dropna())
    assert offset_values, "the demo must carry offsets to local"
    assert all(isinstance(value, float) for value in offset_values)


def test_s2_cell_id_exact_int64(demo: sqlite3.Connection, frame: pd.DataFrame) -> None:
    # The S2 leaf cell exceeds float64's 53-bit integer range; any float detour would
    # silently corrupt it. Compare every projected value to the stored int, exactly.
    stored = dict(demo.execute(
        "SELECT a.observation_id, six.s2_cell_id FROM observation a "
        "LEFT JOIN spatial_integration si "
        "  ON si.spatial_expression_id = a.spatial_expression_id "
        "LEFT JOIN spatial_index six "
        "  ON six.spatial_integration_id = si.spatial_integration_id"))
    for uid, value in zip(frame["row_uid_matlas"], frame["s2_cell_id_matlas"]):
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
    assert ";46.203;" in rendered          # a GNSS fix latitude, unquoted float
    assert ";1.0;" in rendered             # an offset to local, restored to a bare number
    assert '"46.203"' not in rendered
    assert '"1.0"' not in rendered


# --- derived locates ----------------------------------------------------------

def test_record_order_derived_from_the_rank(frame: pd.DataFrame) -> None:
    # The ONE derived column left in the projection. `record_order` is exactly
    # `record_rank == 1`, so a stored copy could only ever contradict the rank beside it —
    # the argument that also deleted Genesis/Locates and, this pass, is_presence (whose
    # answer is read straight off `spatial_link`).
    for rank, order in zip(frame["record_rank_matlas"], frame["record_order_matlas"]):
        if pd.isna(rank):
            assert pd.isna(order)   # no verdict is not a verdict of "additional"
        else:
            assert order == ("main" if int(rank) == 1 else "additional")
    # The demo must actually exercise a rank, or the test proves nothing.
    assert frame["record_rank_matlas"].notna().any()


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
    a_party = frame[frame["source_label_matlas"] == "CDR op-A"].iloc[0]
    record = json.loads(raw[a_party["source_record_uid_matlas"]])
    assert a_party["orig_a_party"] == record["a_party"] == "+41 79 123 45 67"
    assert pd.isna(a_party["orig_ZLATITUDE"])   # a CDR record has no iOS-cache key


def test_passthrough_collision_gets_trailing_underscore() -> None:
    # A source field whose prefixed name collides with a canonical column must be
    # disambiguated (trailing "_"), never allowed to shadow the canonical column.
    # "raw" under the "position_" prefix would render as the canonical "position_raw".
    names = flat_view._passthrough_names(["raw", "raw_"], "position_")
    assert names["raw"] == "position_raw_"        # "position_raw" is canonical
    assert names["raw_"] == "position_raw__"      # and chains must not collide either


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
    frame = pd.DataFrame({"offset_to_local_hours_lower": ["1.0", "N/A", "0.0", None]})
    flat_view._restore_numeric_dtype(frame, "offset_to_local_hours_lower")
    restored = list(frame["offset_to_local_hours_lower"])
    assert restored[0] == 1.0 and isinstance(restored[0], float)   # real offset -> number
    assert restored[1] == "N/A"                                    # sentinel -> kept text
    assert restored[2] == 0.0 and isinstance(restored[2], float)   # real offset -> number
    assert restored[3] is None                                     # empty -> stays empty
    # And under the exporter's QUOTE_NONNUMERIC the numbers come out bare, the sentinel quoted.
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False, sep=";", quoting=csv.QUOTE_NONNUMERIC)
    rendered = buffer.getvalue().splitlines()
    # (an empty cell renders as a quoted "" here, exactly as an all-null offset column does.)
    assert rendered == ['"offset_to_local_hours_lower"', "1.0", '"N/A"', "0.0", '""']
