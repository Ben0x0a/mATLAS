"""Tests for the untangle L3 brick (``model_atlas.analyze.untangle``).

Defines:    the pinned-oracle verdict test (the ``_EXPECTED`` tuple was verified
            against the retired ``transforms.rank.untangle`` before the T60 cut-over,
            so the store-read ``analyse`` still answers to the old engine's exact
            per-assertion ``(record_type, record_rank)``), the decision-19 re-run
            tests (a bumped version ADDS a second row per assertion and the
            projection picks the current method's verdict; the SAME version re-run
            RAISES), the analysis id derivation check, and the empty-store no-op.
Used by:    the T40 verification gate (pytest tests/analyze/test_untangle.py).
Depends on: ``model_atlas.analyze.untangle`` (under test), ``model_atlas.core``
            (``ids`` / ``vocabularies`` / ``store`` / ``projection``), pandas and
            pytest.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from model_atlas.analyze import untangle
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
        "entity_position_link": "at",
        "entity_time_link": "observed_at",
        "spatial_temporal_link": "instant",
    }
    if hacc is not None:
        spec["horizontal_accuracy_m"] = hacc
    spec.update(extra)
    return spec


# The scenario: one same-second group of four (accuracy ranking + a completeness
# tie-break + a missing-accuracy row), a +30 s row (singleton at 1 s resolution,
# group member at 60 s), a same-second row of ANOTHER entity (singleton — entity is
# part of the grouping key), and a corner-less assertion (ineligible — no times).
_SPECS: tuple[dict, ...] = (
    _placement(_SECOND_US + 100_000, hacc=12.0),                        # r0 -> rank 3
    _placement(_SECOND_US + 200_000, hacc=5.0),                         # r1 -> rank 2
    _placement(_SECOND_US + 300_000),                                   # r2 -> rank 4 (no hacc)
    _placement(_SECOND_US + 400_000, hacc=5.0, altitude_m=430.0,        # r3 -> rank 1 (ties r1
               temporal_source="NTP", utc_offset_hours=1.0,             #  on hacc, wins on
               time_accuracy_us=1_000_000),                             #  completeness)
    _placement(_SECOND_US + 30_000_000, hacc=1.0),                      # r4 -> singleton (+30 s)
    _placement(_SECOND_US + 500_000, hacc=2.0, entity="+41760000000"),  # r5 -> other entity
    {"entity": "+41791234567"},                                         # r6 -> no times at all
)

# The oracle verdicts, hard-coded: a both-paths-empty parity pass would be trivial,
# so the OLD engine's expected output is pinned explicitly too.
_EXPECTED: tuple[tuple[str | None, int | None], ...] = (
    ("additional", 3), ("additional", 2), ("additional", 4), ("main", 1),
    (None, None), (None, None), (None, None),
)


def _write_spec(store: RunStore, *, source_id: str, preset_id: str, preset_key: str,
                line_no: int, spec: dict) -> str:
    """Write one spec into its settled relational homes; return the assertion id."""
    rr_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(raw_record_id=rr_id, source_id=source_id,
                                       line_no=line_no, raw_json="{}"))

    identifier = spec.get("entity")
    entity_id = ids.entity_id((rr_id, 0, identifier))
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type=(V.IdentifierType.MSISDN if identifier
                         else V.IdentifierType.NONE),
        type=V.EntityType.DEVICE, concerns=spec.get("linked_entity")))

    tx_id = None
    if "time_lower_unix_utc_us" in spec:
        tx_id = ids.temporal_expression_id((rr_id, 0))
        store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind=V.TemporalKind.INTERVAL,
            referential=V.TemporalReferential.UNIX_EPOCH_US,
            temporal_source=spec.get("temporal_source")))
        for ordinal, part in enumerate(("lower", "upper")):
            store.write_temporal_component(V.TemporalComponent(
                temporal_component_id=ids.temporal_component_id(tx_id, ordinal),
                temporal_expression_id=tx_id, ordinal=ordinal, part=part,
                source_field=spec.get(f"time_{part}_source_field"),
                raw_value=spec.get(f"time_{part}_raw")))
        store.write_timespan_integration(V.TimespanIntegration(
            timespan_integration_id=ids.timespan_integration_id(tx_id, "epoch_unix_us/1.0"),
            temporal_expression_id=tx_id, method="epoch_unix_us/1.0",
            lower_unix_utc_us=spec.get("time_lower_unix_utc_us"),
            upper_unix_utc_us=spec.get("time_upper_unix_utc_us"),
            utc_offset_hours_lower=spec.get("utc_offset_hours"),
            # the spec's single accuracy feeds the lower bound; untangle COALESCEs the two
            # per-bound columns back into the one completeness bit the old column carried.
            lower_accuracy_us=spec.get("time_accuracy_us")))

    sx_id = None
    if "latitude_wgs84" in spec:
        sx_id = ids.spatial_expression_id((rr_id, 0))
        store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx_id, referential=V.SpatialReferential.EPSG_4326,
            spatial_source=spec.get("position_source")))
        store.write_spatial_integration(V.SpatialIntegration(
            spatial_integration_id=ids.spatial_integration_id(sx_id, "passthrough_wgs84/1.0"),
            spatial_expression_id=sx_id, method="passthrough_wgs84/1.0",
            latitude_wgs84=spec.get("latitude_wgs84"),
            longitude_wgs84=spec.get("longitude_wgs84"),
            altitude_m=spec.get("altitude_m"),
            horizontal_accuracy_m=spec.get("horizontal_accuracy_m")))

    assertion_id = ids.assertion_id(rr_id, preset_key, 0)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=rr_id, preset_id=preset_id,
        output_ordinal=0, entity_id=entity_id, content_hash=f"ch-{line_no}",
        claim_digest=f"cd-{line_no}", spatial_expression_id=sx_id,
        temporal_expression_id=tx_id,
        entity_position_link=spec.get("entity_position_link"),
        entity_time_link=spec.get("entity_time_link"),
        spatial_temporal_link=spec.get("spatial_temporal_link")))
    return assertion_id


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
    source_id = ids.source_id("/case/track.csv", None, None)
    store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                input_file_path="/case/track.csv"))
    return [
        _write_spec(store, source_id=source_id, preset_id=preset_id,
                    preset_key="gps_generic", line_no=line_no, spec=spec)
        for line_no, spec in enumerate(_SPECS, start=1)
    ]


def _analysis_verdicts(store: RunStore, method: str) -> dict[str, tuple]:
    return {
        assertion_id: (record_type, record_rank)
        for assertion_id, record_type, record_rank in store.connection.execute(
            "SELECT assertion_id, record_type, record_rank FROM analysis "
            "WHERE method = ?", (method,))
    }


# --- parity: the store-read path matches the pinned old-engine oracle ---------

def test_parity_with_the_pinned_old_engine_oracle() -> None:
    with RunStore() as store:
        assertion_ids = _build_scenario(store)
        written = untangle.analyse(store)
        assert written == len(_SPECS)   # one verdict row per assertion, ranked or not
        new_verdicts = _analysis_verdicts(store, "untangle/2.0")

    assert tuple(new_verdicts[assertion_id] for assertion_id in assertion_ids) == _EXPECTED


def test_analysis_id_is_the_core_derivation() -> None:
    with RunStore() as store:
        assertion_ids = _build_scenario(store)
        untangle.analyse(store)
        stored = dict(store.connection.execute(
            "SELECT assertion_id, analysis_id FROM analysis"))
    for assertion_id in assertion_ids:
        assert stored[assertion_id] == ids.analysis_id(assertion_id, "untangle/2.0")


# --- decision 19: a re-run ADDS rows; the projection reads the current method --

def test_bumped_version_adds_rows_and_projection_picks_current() -> None:
    with RunStore() as store:
        assertion_ids = _build_scenario(store)
        untangle.analyse(store)   # untangle/2.0 at 1 s resolution
        # The "changed ranker": a 60 s grouping resolution under a bumped version.
        # r4 (+30 s) now joins the group and, carrying the best accuracy, wins it.
        untangle.analyse(store, version="2.1", resolution_us=60_000_000)

        # ADDED, never mutated: two verdict rows per assertion, one per method.
        assert store.connection.execute(
            "SELECT COUNT(*) FROM analysis").fetchone()[0] == 2 * len(_SPECS)
        for assertion_id in assertion_ids:
            methods = {row[0] for row in store.connection.execute(
                "SELECT method FROM analysis WHERE assertion_id = ?", (assertion_id,))}
            assert methods == {"untangle/2.0", "untangle/2.1"}

        # The projection's default is the CURRENT method (MAX(method) = 2.1)…
        current = flat_view.build_flat_view(
            store.connection, include_source_columns=False).set_index("row_uid")
        r4 = assertion_ids[4]
        assert current.at[r4, "record_type"] == "main"
        assert int(current.at[r4, "record_rank"]) == 1
        # …while the superseded generation stays readable by pinning its method.
        pinned = flat_view.build_flat_view(
            store.connection, include_source_columns=False,
            ranking_method="untangle/2.0").set_index("row_uid")
        assert pd.isna(pinned.at[r4, "record_type"])
        assert current.at[assertion_ids[3], "record_type"] == "additional"  # r3 demoted by r4
        assert pinned.at[assertion_ids[3], "record_type"] == "main"


def test_same_version_rerun_raises() -> None:
    # UNIQUE(assertion_id, method): overwriting a recorded verdict in place would be
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
