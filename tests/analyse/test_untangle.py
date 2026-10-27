"""Tests for the untangle L3 brick (``model_atlas.analyse.untangle``).

Defines:    the pinned-oracle verdict test (the ``_EXPECTED`` tuple was verified
            against the retired ``transforms.rank.untangle`` before the cut-over to the
            relational engine, so the store-read ``analyse`` still answers to the old
            engine's exact per-observation ``record_rank``), the
            re-run tests (a bumped version ADDS a second row per observation and the
            projection picks the current method's verdict; the SAME version re-run
            RAISES), the analysis id derivation check, and the empty-store no-op.
Used by:    the verification gate (pytest tests/analyse/test_untangle.py).
Depends on: ``model_atlas.analyse.untangle`` (under test), ``model_atlas.core``
            (``ids`` / ``vocabularies`` / ``store`` / ``flat_view``), pandas and
            pytest.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from model_atlas.analyse import untangle
from model_atlas.core import ids
from model_atlas.core import flat_view
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore

# --- ONE fixture spec + a pinned oracle --------------------------------------
# Each spec is a dict of OLD flat column names -> values, written into their settled
# relational homes and ranked by ``analyse``. ``_EXPECTED`` is the verdict tuple the
# retired ``transforms.rank.untangle`` produced for the SAME specs (verified while
# both engines coexisted), so the oracle survives the old engine's deletion.

_SECOND_US = 1_700_000_000_000_000   # 2023-11-14T22:13:20Z, in microseconds


def _placement(lower_us: int, hacc: float | None = None, **extra: object) -> dict:
    """A GPS-fix-shaped spec: same entity/source-fields, an instant at ``lower_us``."""
    spec: dict = {
        "entity": "+41791234567",
        "time_lower_raw": str(lower_us),
        "time_lower_source_field": "ts",
        "time_lower_unix_utc_us": lower_us,
        "time_upper_raw": str(lower_us),
        "time_upper_source_field": "ts",
        "time_upper_unix_utc_us": lower_us,
        "latitude_wgs84": 46.2,
        "longitude_wgs84": 6.1,
        "spatial_link": "measured",
        "temporal_link": "at_least_once",
    }
    if hacc is not None:
        spec["horizontal_accuracy_m"] = hacc
    spec.update(extra)
    return spec


# The scenario: one same-second group of four (accuracy ranking + a completeness
# tie-break + a missing-accuracy row), a +30 s row (singleton at 1 s resolution,
# group member at 60 s), a same-second row of ANOTHER entity (singleton — entity is
# part of the grouping key), and a corner-less observation (ineligible — no times).
_SPECS: tuple[dict, ...] = (
    _placement(_SECOND_US + 100_000, hacc=12.0),                        # r0 -> rank 3
    _placement(_SECOND_US + 200_000, hacc=5.0),                         # r1 -> rank 2
    _placement(_SECOND_US + 300_000),                                   # r2 -> rank 4 (no hacc)
    _placement(_SECOND_US + 400_000, hacc=5.0, altitude_m=430.0,        # r3 -> rank 1 (ties r1
               temporal_source="NTP", offset_to_local_hours=1.0,       #  on hacc, wins on
               time_accuracy_us=1_000_000),                             #  completeness)
    _placement(_SECOND_US + 30_000_000, hacc=1.0),                      # r4 -> singleton (+30 s)
    _placement(_SECOND_US + 500_000, hacc=2.0, entity="+41760000000"),  # r5 -> other entity
    {"entity": "+41791234567"},                                         # r6 -> no times at all
)

# The oracle verdicts, hard-coded: a both-paths-empty parity pass would be trivial,
# so the OLD engine's expected output is pinned explicitly too.
# The main/additional label is no longer stored — it is exactly ``record_rank == 1`` and the
# projection derives ``record_order_matlas`` from it — so the oracle pins the rank alone.
_EXPECTED: tuple[int | None, ...] = (3, 2, 4, 1, None, None, None)


def _write_spec(store: RunStore, *, source_file_id: str, preset_id: str, preset_key: str,
                line_no: int, spec: dict) -> str:
    """Write one spec into its settled relational homes; return the observation id."""
    rr_id = ids.new_id()
    store.write_raw_record(V.RawRecord(raw_record_id=rr_id, source_file_id=source_file_id,
                                       line_no=line_no, raw_json="{}"))

    identifier = spec.get("entity")
    entity_id = ids.new_id()
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type=(V.IdentifierType.MSISDN if identifier
                         else V.IdentifierType.NONE),
        type=V.EntityType.DEVICE, concerns=spec.get("concerns")))

    tx_id = None
    if "time_lower_unix_utc_us" in spec:
        tx_id = ids.new_id()
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind=V.TemporalKind.INTERVAL,
            referential=V.TemporalReferential.UNIX_EPOCH_US,
            temporal_source=spec.get("temporal_source")))
        for ordinal, part in enumerate(("lower", "upper")):
            store.write_temporal_component(V.TemporalComponent(
                temporal_component_id=ids.new_id(),
                temporal_expression_id=tx_id, ordinal=ordinal, part=part,
                source_field=spec.get(f"time_{part}_source_field"),
                raw_value=spec.get(f"time_{part}_raw")))
        store.write_timespan_integration(V.TimespanIntegration(
            timespan_integration_id=ids.new_id(),
            temporal_expression_id=tx_id, method="epoch_unix_us/1.0",
            lower_unix_utc_us=spec.get("time_lower_unix_utc_us"),
            upper_unix_utc_us=spec.get("time_upper_unix_utc_us"),
            offset_to_local_hours_lower=spec.get("offset_to_local_hours"),
            # the spec's single accuracy feeds the lower bound; untangle COALESCEs the two
            # per-bound columns back into the one completeness bit the old column carried.
            lower_accuracy_us=spec.get("time_accuracy_us")))

    sx_id = None
    if "latitude_wgs84" in spec:
        sx_id = ids.new_id()
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx_id,
        position_type=V.PositionType.COORDINATE, referential=V.SpatialReferential.EPSG_4326,
            spatial_source=spec.get("position_source")))
        store.write_spatial_integration(V.SpatialIntegration(
            spatial_integration_id=ids.new_id(),
            spatial_expression_id=sx_id, method="passthrough_wgs84/1.0",
            latitude_wgs84=spec.get("latitude_wgs84"),
            longitude_wgs84=spec.get("longitude_wgs84"),
            altitude_m=spec.get("altitude_m"),
            horizontal_accuracy_m=spec.get("horizontal_accuracy_m")))

    observation_id = ids.new_id()
    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=rr_id, preset_id=preset_id, entity_id=entity_id, observation_content_hash=f"ch-{line_no}",
        spatiotemporal_entity_hash=f"cd-{line_no}", spatial_expression_id=sx_id,
        temporal_expression_id=tx_id,
        spatial_link=spec.get("spatial_link"),
        temporal_link=spec.get("temporal_link")))
    return observation_id


def _build_scenario(store: RunStore) -> list[str]:
    """Write the whole spec scenario (L0 scaffolding + one placement per spec)."""
    run_id = ids.run_id()
    store.write_case_metadata(V.CaseMetadata(case_id=ids.case_metadata_id(),
                                             case_number="GE-2026-04412"))
    store.write_run(V.Run(run_id=run_id,
                          case_id=store.connection.execute(
                              "SELECT case_id FROM case_metadata").fetchone()[0]))
    preset_id = ids.preset_id("gps_generic", "1.0")
    store.write_preset(V.Preset(preset_id=preset_id, key="gps_generic",
                                name="gps_generic", version="1.0",
                                definition_digest="digest-1"))
    source_file_id = ids.new_id()
    store.write_source_file(V.SourceFile(source_file_id=source_file_id, run_id=run_id,
                                input_file_path="/case/track.csv"))
    return [
        _write_spec(store, source_file_id=source_file_id, preset_id=preset_id,
                    preset_key="gps_generic", line_no=line_no, spec=spec)
        for line_no, spec in enumerate(_SPECS, start=1)
    ]


def _analysis_verdicts(store: RunStore, method: str) -> dict[str, tuple]:
    return {
        observation_id: record_rank
        for observation_id, record_rank in store.connection.execute(
            "SELECT observation_id, record_rank FROM analysis "
            "WHERE method = ?", (method,))
    }


# --- parity: the store-read path matches the pinned old-engine oracle ---------

def test_parity_with_the_pinned_old_engine_oracle() -> None:
    with RunStore() as store:
        observation_ids = _build_scenario(store)
        written = untangle.analyse(store)
        assert written == len(_SPECS)   # one verdict row per observation, ranked or not
        new_verdicts = _analysis_verdicts(store, "untangle/2.0")

    assert tuple(new_verdicts[observation_id] for observation_id in observation_ids) == _EXPECTED


def test_analysis_rows_are_one_per_observation_and_method() -> None:
    """Ids are random (decision 50); what the analysis table guarantees is
    UNIQUE(observation_id, method) — one verdict per observation per method."""
    with RunStore() as store:
        observation_ids = _build_scenario(store)
        untangle.analyse(store)
        stored = dict(store.connection.execute(
            "SELECT observation_id, analysis_id FROM analysis"))
    assert set(stored) == set(observation_ids)          # exactly one row per observation
    assert len(set(stored.values())) == len(observation_ids)   # and distinct ids


# --- decision 19: a re-run ADDS rows; the projection reads the current method --

def test_bumped_version_adds_rows_and_projection_picks_current() -> None:
    with RunStore() as store:
        observation_ids = _build_scenario(store)
        untangle.analyse(store)   # untangle/2.0 at 1 s resolution
        # The "changed ranker": a 60 s grouping resolution under a bumped version.
        # r4 (+30 s) now joins the group and, carrying the best accuracy, wins it.
        untangle.analyse(store, version="2.1", resolution_us=60_000_000)

        # ADDED, never mutated: two verdict rows per observation, one per method.
        assert store.connection.execute(
            "SELECT COUNT(*) FROM analysis").fetchone()[0] == 2 * len(_SPECS)
        for observation_id in observation_ids:
            methods = {row[0] for row in store.connection.execute(
                "SELECT method FROM analysis WHERE observation_id = ?", (observation_id,))}
            assert methods == {"untangle/2.0", "untangle/2.1"}

        # The projection's default is the CURRENT method (MAX(method) = 2.1)…
        current = flat_view.build_flat_view(
            store.connection, include_source_columns=False).set_index("row_uid_matlas")
        r4 = observation_ids[4]
        assert current.at[r4, "record_order_matlas"] == "main"
        assert int(current.at[r4, "record_rank_matlas"]) == 1
        # …while the superseded generation stays readable by pinning its method.
        pinned = flat_view.build_flat_view(
            store.connection, include_source_columns=False,
            ranking_method="untangle/2.0").set_index("row_uid_matlas")
        assert pd.isna(pinned.at[r4, "record_order_matlas"])
        # r3 demoted by r4
        assert current.at[observation_ids[3], "record_order_matlas"] == "additional"
        assert pinned.at[observation_ids[3], "record_order_matlas"] == "main"


def test_same_version_rerun_raises() -> None:
    # UNIQUE(observation_id, method): overwriting a recorded verdict in place would be
    # silent loss, so the same method generation may never be written twice.
    with RunStore() as store:
        _build_scenario(store)
        untangle.analyse(store)
        with pytest.raises(sqlite3.IntegrityError):
            untangle.analyse(store)


def test_empty_store_is_a_no_op() -> None:
    with RunStore() as store:
        assert untangle.analyse(store) == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM analysis").fetchone()[0] == 0
