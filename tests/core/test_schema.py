"""Tests for the derived run-database DDL (``model_atlas.core.schema``).

Defines:    the schema build/integrity test, the demo-fixture load test (proves the
            derived DDL accepts the settled example DB), the golden DDL snapshot, and the
            §2a / corner / edit-log UNIQUE guards + the enum CHECK rejection.
Used by:    the verification gate (pytest tests/core/test_schema.py).
Depends on: ``model_atlas.core.schema`` and the checked-in fixture
            ``docs/examples/matlas_example.sqlite`` + golden ``tests/core/schema.gol.sql``.

The golden is a checked-in file (``schema.gol.sql``) rather than an in-test constant: the
DDL is long and a file lets a reviewer diff a schema change directly, and it keeps the
test module readable. Set ``UPDATE_GOLDEN=1`` in the environment to regenerate it after a
deliberate change.
"""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from model_atlas.core import schema
from model_atlas.core import vocabularies as V

_HERE = Path(__file__).resolve().parent
_GOLDEN = _HERE / "schema.gol.sql"
_DEMO_DB = _HERE.parents[1] / "docs" / "examples" / "matlas_example.sqlite"

# Every table the schema builds except matlas_meta, in FK-safe (parent-first) order.
_COPYABLE = schema.COPYABLE_TABLES


def _memory_conn() -> sqlite3.Connection:
    # uri=True so the demo-load test can ATTACH the fixture read-only (mode=ro); harmless
    # for the plain :memory: DB the other tests use.
    connection = sqlite3.connect(":memory:", uri=True)
    schema.create_schema(connection)
    return connection


# --- build / integrity -----------------------------------------------------

def test_schema_builds_and_is_consistent() -> None:
    connection = _memory_conn()
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA user_version").fetchone()[0] == schema.USER_VERSION
    assert (connection.execute("PRAGMA application_id").fetchone()[0]
            == schema.APPLICATION_ID)


def test_all_tables_and_views_present() -> None:
    connection = _memory_conn()
    tables = {r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    views = {r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}
    # matlas_meta and model_description are metadata, not evidence, so neither is copyable.
    expected_tables = set(_COPYABLE) | {"matlas_meta", "model_description"}
    assert tables == expected_tables
    assert views == {"duplicate_sources", "possible_same_evidence"}
    # exactly one matlas_meta row, stamped with the schema/families versions.
    assert connection.execute("SELECT COUNT(*) FROM matlas_meta").fetchone()[0] == 1


def test_model_description_describes_every_table_and_field() -> None:
    """The DB must explain itself: one row per table, one per field, nothing missing.

    This is what replaced the per-column DDL comment. SQLite has no structured column
    attribute, so without these rows a persisted run DB cannot say which of its columns
    mATLAS added rather than read from the trace.
    """
    connection = _memory_conn()
    described = {(t, f) for t, f in connection.execute(
        "SELECT table_name, field_name FROM model_description")}
    expected: set[tuple[str, str | None]] = set()
    for spec in schema._table_specs():
        expected.add((spec["name"], None))
        expected |= {(spec["name"], f.name)
                     for f in dataclasses.fields(spec["dataclass"])}
    assert described == expected

    # A table row carries no type/unit/enrichment — it describes the table, not a column.
    table_rows = connection.execute(
        "SELECT type, unit, matlas_enrichment FROM model_description "
        "WHERE field_name IS NULL").fetchall()
    assert table_rows and all(r == (None, None, None) for r in table_rows)
    # Every description is real prose, not an empty placeholder.
    assert connection.execute(
        "SELECT COUNT(*) FROM model_description "
        "WHERE description IS NULL OR TRIM(description) = ''").fetchone()[0] == 0


def test_model_description_enrichment_matches_the_model_marker() -> None:
    """The stored flag must equal the field's own marker — one source, no second copy."""
    connection = _memory_conn()
    for table, field_name, flag in connection.execute(
            "SELECT table_name, field_name, matlas_enrichment FROM model_description "
            "WHERE field_name IS NOT NULL"):
        model = next(s["dataclass"] for s in schema._table_specs() if s["name"] == table)
        assert bool(flag) == V.is_enrichment(model, field_name), f"{table}.{field_name}"


def test_matlas_meta_single_row_values() -> None:
    connection = _memory_conn()
    schema_version, families_version = connection.execute(
        "SELECT schema_version, families_version FROM matlas_meta").fetchone()
    assert schema_version == schema.SCHEMA_VERSION
    assert families_version == schema.FAMILIES_VERSION


# --- golden snapshot -------------------------------------------------------

def test_ddl_matches_golden() -> None:
    generated = "\n\n".join(schema.schema_statements()) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        _GOLDEN.write_text(generated, encoding="utf-8")
    assert _GOLDEN.exists(), (
        "golden missing; regenerate with UPDATE_GOLDEN=1 pytest tests/core/test_schema.py")
    assert generated == _GOLDEN.read_text(encoding="utf-8"), (
        "DDL drifted from the golden; if intended, regenerate with "
        "UPDATE_GOLDEN=1 pytest tests/core/test_schema.py")


# --- demo-fixture load (the derived DDL accepts the settled fixture) -------

def test_loads_demo_fixture_with_clean_fk_check() -> None:
    assert _DEMO_DB.exists(), f"demo fixture missing at {_DEMO_DB}"
    connection = _memory_conn()
    connection.execute(f"ATTACH DATABASE 'file:{_DEMO_DB}?mode=ro' AS demo")

    mismatches: list[str] = []
    for table in _COPYABLE:
        fresh_cols = [r[1] for r in connection.execute(f"PRAGMA table_info({table})")]
        demo_cols = [r[1] for r in connection.execute(f"PRAGMA demo.table_info({table})")]
        only_fresh = set(fresh_cols) - set(demo_cols)
        only_demo = set(demo_cols) - set(fresh_cols)
        if only_fresh or only_demo:
            mismatches.append(
                f"{table}: only_in_schema={sorted(only_fresh)} "
                f"only_in_demo={sorted(only_demo)}")
        # copy by shared column name so a column-order difference cannot corrupt the load.
        common = [c for c in fresh_cols if c in demo_cols]
        col_list = ", ".join(common)
        connection.execute(
            f"INSERT INTO main.{table} ({col_list}) SELECT {col_list} FROM demo.{table}")

    # Loud report of any column drift between the derived DDL and the settled fixture.
    assert not mismatches, "column mismatch(es) between schema and demo:\n" + "\n".join(
        mismatches)
    # The whole point: FK integrity holds after loading every settled row.
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    # sanity: the fixture actually had rows (a silently empty load would pass FK check).
    assert connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0] > 0
    connection.close()


# --- §2a and the owned-corner / edit-log UNIQUE guards ---------------------

def _seed_one_observation(connection: sqlite3.Connection) -> dict[str, str]:
    """Insert the minimal parent chain + one observation; return the ids used."""
    ids = {name: str(uuid.uuid4()) for name in (
        "case", "run", "preset", "source", "record", "entity", "sx", "tx", "observation")}
    connection.execute("INSERT INTO case_metadata (case_id, case_number) VALUES (?, ?)",
                       (ids["case"], "C-1"))
    connection.execute("INSERT INTO run (run_id, case_id) VALUES (?, ?)",
                       (ids["run"], ids["case"]))
    connection.execute(
        "INSERT INTO preset (preset_id, key, definition_digest) VALUES (?, ?, ?)",
        (ids["preset"], "k", "digest"))
    connection.execute(
        "INSERT INTO source_file (source_file_id, run_id, input_file_path) VALUES (?, ?, ?)",
        (ids["source"], ids["run"], "/f.csv"))
    connection.execute(
        "INSERT INTO raw_record (raw_record_id, source_file_id, line_no, raw_json) "
        "VALUES (?, ?, ?, ?)", (ids["record"], ids["source"], 1, "{}"))
    connection.execute(
        "INSERT INTO entity (entity_id, identifier_type, type) VALUES (?, ?, ?)",
        (ids["entity"], "none", "device"))
    connection.execute(
        "INSERT INTO spatial_expression "
        "(spatial_expression_id, position_type, referential) "
        "VALUES (?, ?, ?)", (ids["sx"], "coordinate", "epsg:4326"))
    connection.execute(
        "INSERT INTO temporal_expression (temporal_expression_id, kind) VALUES (?, ?)",
        (ids["tx"], "instant"))
    connection.execute(
        "INSERT INTO observation (observation_id, raw_record_id, preset_id, "
        "entity_id, spatial_expression_id, temporal_expression_id, observation_content_hash, "
        "spatiotemporal_entity_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ids["observation"], ids["record"], ids["preset"], ids["entity"],
         ids["sx"], ids["tx"], "ch", "cd"))
    return ids


def test_several_observations_per_record_and_preset_are_legal() -> None:
    """There is NO UNIQUE over (raw_record_id, preset_id) since decision 50.

    One preset with several observation templates legitimately writes several observations
    for one record — the route search's origin and destination — and `output_ordinal`, which
    used to discriminate them, was removed with the derived ids. Each row still OWNS its
    corners, which is what the remaining UNIQUEs enforce (see the two tests below).

    Re-ingesting a source is prevented earlier, by the pipeline's append check on the
    source's natural key; it was never this constraint's job.
    """
    connection = _memory_conn()
    ids = _seed_one_observation(connection)
    second_entity = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO entity (entity_id, identifier_type, type) VALUES (?, ?, ?)",
        (second_entity, "none", "device"))
    connection.execute(
        "INSERT INTO observation (observation_id, raw_record_id, preset_id, "
        "entity_id, observation_content_hash, spatiotemporal_entity_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), ids["record"], ids["preset"], second_entity, "ch2", "cd2"))
    (n,) = connection.execute(
        "SELECT COUNT(*) FROM observation WHERE raw_record_id = ?", (ids["record"],)).fetchone()
    assert n == 2


def test_shared_entity_corner_raises() -> None:
    connection = _memory_conn()
    ids = _seed_one_observation(connection)
    # A second observation referencing the SAME entity row violates UNIQUE(entity_id).
    connection.execute(
        "INSERT INTO raw_record (raw_record_id, source_file_id, line_no, raw_json) "
        "VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), ids["source"], 2, "{}"))
    other_record = connection.execute(
        "SELECT raw_record_id FROM raw_record WHERE line_no = 2").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO observation (observation_id, raw_record_id, preset_id, "
            "entity_id, observation_content_hash, spatiotemporal_entity_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), other_record, ids["preset"],
             ids["entity"], "ch3", "cd3"))


def test_shared_spatial_corner_raises() -> None:
    connection = _memory_conn()
    ids = _seed_one_observation(connection)
    connection.execute(
        "INSERT INTO raw_record (raw_record_id, source_file_id, line_no, raw_json) "
        "VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), ids["source"], 2, "{}"))
    other_record = connection.execute(
        "SELECT raw_record_id FROM raw_record WHERE line_no = 2").fetchone()[0]
    # Reusing the same spatial_expression_id violates its owned-corner UNIQUE.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO observation (observation_id, raw_record_id, preset_id, "
            "entity_id, spatial_expression_id, observation_content_hash, "
            "spatiotemporal_entity_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), other_record, ids["preset"],
             str(uuid.uuid4()), ids["sx"], "ch4", "cd4"))


def test_duplicate_edit_log_ordinal_raises() -> None:
    connection = _memory_conn()
    connection.execute(
        "INSERT INTO edit_log (edit_id, ordinal, edit_kind) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), 1, "update"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO edit_log (edit_id, ordinal, edit_kind) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), 1, "new_row"))


# --- CHECK rejection -------------------------------------------------------

def test_invalid_enum_value_rejected() -> None:
    connection = _memory_conn()
    # 'delete' is not a member of EditKind (update | new_row) — the derived
    # CHECK must reject it, so an out-of-vocabulary value can never reach the DB.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO edit_log (edit_id, ordinal, edit_kind) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), 5, "delete"))


def test_valid_enum_value_accepted() -> None:
    connection = _memory_conn()
    connection.execute(
        "INSERT INTO edit_log (edit_id, ordinal, edit_kind) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), 7, "new_row"))
    assert connection.execute(
        "SELECT edit_kind FROM edit_log WHERE ordinal = 7").fetchone()[0] == "new_row"


# --- derived enum-CHECK coverage + NOT-NULL primary keys (SS2 / SS4) --------

def test_every_enum_typed_column_has_a_derived_check() -> None:
    """SS2: coverage is DERIVED, not a hand-kept map — so every Enum-typed dataclass field
    carries a membership CHECK in the DDL and a future Enum column cannot silently ship
    unconstrained. Drives the same column->Enum registry (_enum_of) the builder uses."""
    ddl = "\n".join(schema.schema_statements())
    checked = 0
    for spec in schema._table_specs():
        hints = schema._hints(spec["dataclass"])
        for name, annotation in hints.items():
            if schema._enum_of(annotation) is not None:
                assert f"CHECK ({name} IN (" in ddl, (
                    f"{spec['name']}.{name} is Enum-typed but has no derived CHECK")
                checked += 1
    assert checked > 0, "expected at least one Enum-typed column to be covered"


def test_null_text_primary_key_is_rejected() -> None:
    """SS4: a TEXT (non-INTEGER) PRIMARY KEY permits NULL in SQLite unless marked NOT NULL;
    the derived DDL now marks every TEXT pk NOT NULL, so a None/NULL id fails at write time."""
    connection = _memory_conn()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO case_metadata (case_id, case_number) VALUES (?, ?)",
            (None, "C-NULL"))
