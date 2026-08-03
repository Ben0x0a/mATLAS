"""Tests for model_atlas/app/concomitance_run.py (the Verify list).

Covers:     the adapter raises ``ConcomitanceNotYetSupported`` for each of the four
            unsupported shapes (two cases, asymmetric window, accuracy, different
            groups) with the stated message, and runs for real on the supported shape
            (one case, identical/empty groups, symmetric window) — including progress
            callbacks, cooperative cancellation (partial count, file kept), and a
            written CSV whose rows carry position/time absent from the raw engine
            output.
Uses:       ``model_atlas.app.concomitance_run`` + the same fixture-building helpers as
            tests/analyze/test_concomitance.py (real layer-typed store writers, real
            ``integrate_spatial``), persisted to a real file so the read-only URI-safe
            opener is exercised exactly as the GUI/CLI will use it.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from model_atlas.analyze.concomitance import EARTH_RADIUS_M
from model_atlas.analyze.concomitance_request import (
    ConcomitanceRequest,
    Criterion,
    CriterionKind,
    EntityGroup,
    MatchMode,
)
from model_atlas.app.concomitance_run import ConcomitanceNotYetSupported, run_concomitance
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial

KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0


def _north_of(metres: float) -> tuple[float, float]:
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


def _add_point(store: RunStore, line_no: int, source_id: str, preset_id: str,
               preset_key: str, latitude: float, longitude: float,
               lower_us: int, upper_us: int, *, identifier: str) -> str:
    record_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=record_id, source_id=source_id, line_no=line_no, raw_json="{}"))
    natural_key = (record_id, 0)
    entity_id = ids.entity_id(natural_key)
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type="msisdn", type="device"))
    sx_id = ids.spatial_expression_id(natural_key)
    store.write_spatial_expression(V.SpatialExpression(
        spatial_expression_id=sx_id, referential="epsg:4326"))
    for ordinal, (part, raw) in enumerate((
            ("latitude", str(latitude)), ("longitude", str(longitude)))):
        store.write_spatial_component(V.SpatialComponent(
            spatial_component_id=ids.spatial_component_id(sx_id, ordinal),
            spatial_expression_id=sx_id, ordinal=ordinal, part=part, raw_value=raw))
    tx_id = ids.temporal_expression_id(natural_key)
    store.write_temporal_expression(V.TemporalExpression(
        temporal_expression_id=tx_id, kind="interval"))
    store.write_timespan_integration(V.TimespanIntegration(
        timespan_integration_id=ids.timespan_integration_id(tx_id, "fixture/1.0"),
        temporal_expression_id=tx_id, method="fixture/1.0",
        lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us,
        lower_datetime_iso=dt.datetime.fromtimestamp(
            lower_us / 1_000_000, tz=dt.timezone.utc).isoformat(),
        upper_datetime_iso=dt.datetime.fromtimestamp(
            upper_us / 1_000_000, tz=dt.timezone.utc).isoformat()))
    assertion_id = ids.assertion_id(record_id, preset_key, 0)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=record_id, preset_id=preset_id,
        output_ordinal=0, entity_id=entity_id,
        content_hash=f"ch-{line_no}", claim_digest=f"cd-{line_no}",
        spatial_expression_id=sx_id, temporal_expression_id=tx_id,
        entity_role="subject", entity_position_link="at",
        entity_time_link="observed_at", spatial_temporal_link="instant",
        source_label=f"src-{line_no}"))
    return assertion_id


def _build_case(path: Path) -> tuple[str, str]:
    """Persist a case with two presence assertions 10 m apart, overlapping in time."""
    with RunStore() as store:
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="T101a"))
        run_id = ids.run_id()
        store.write_run(V.Run(run_id=run_id, case_id=case_id))
        source_id = ids.source_id("/case/t101a.csv", None, None)
        store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                    input_file_path="/case/t101a.csv"))
        preset_key = "t101a"
        preset_id = ids.preset_id(preset_key, "1")
        store.write_preset(V.Preset(preset_id=preset_id, key=preset_key, name=None,
                                    version="1", definition_digest="sha256-fixture"))
        a = _add_point(store, 1, source_id, preset_id, preset_key, *KNOWN,
                      BASE_US, BASE_US + 5 * 60_000_000, identifier="+41790000001")
        b = _add_point(store, 2, source_id, preset_id, preset_key, *_north_of(10.0),
                      BASE_US + 2 * 60_000_000, BASE_US + 6 * 60_000_000,
                      identifier="+41790000002")
        integrate_spatial(store)
        store.persist(path)
    return a, b


def _request(tmp_path: Path, db_path: Path, **overrides: object) -> ConcomitanceRequest:
    defaults: dict[str, object] = dict(
        case_1=db_path,
        case_2=None,
        group_1=EntityGroup(),
        group_2=EntityGroup(),
        space_threshold_m=50.0,
        time_before=dt.timedelta(minutes=10),
        time_after=dt.timedelta(minutes=10),
        output_path=tmp_path / "out.csv",
        use_accuracy=False,
    )
    defaults.update(overrides)
    return ConcomitanceRequest(**defaults)


# --- the four unsupported shapes -------------------------------------------------------

def test_two_cases_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path, case_2=db_path)
    with pytest.raises(ConcomitanceNotYetSupported, match="Comparing two cases"):
        run_concomitance(request)


def test_asymmetric_window_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path,
                       time_before=dt.timedelta(minutes=5),
                       time_after=dt.timedelta(minutes=30))
    with pytest.raises(ConcomitanceNotYetSupported, match="asymmetric time window"):
        run_concomitance(request)


def test_use_accuracy_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path, use_accuracy=True)
    with pytest.raises(ConcomitanceNotYetSupported, match="Accuracy-aware matching"):
        run_concomitance(request)


def test_different_groups_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(
        tmp_path, db_path,
        group_1=EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE, "device"),)),
        group_2=EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),)))
    with pytest.raises(ConcomitanceNotYetSupported, match="Two different entity groups"):
        run_concomitance(request)


def test_cross_kind_any_raises(tmp_path: Path) -> None:
    """A group mode still cannot be translated even when group_1 == group_2."""
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    group = EntityGroup(criteria=(
        Criterion(CriterionKind.ENTITY_TYPE, "device"),
        Criterion(CriterionKind.IDENTIFIER_TYPE, "plate")), mode=MatchMode.ANY)
    request = _request(tmp_path, db_path, group_1=group, group_2=group)
    with pytest.raises(ConcomitanceNotYetSupported, match="spanning more than one kind"):
        run_concomitance(request)


# --- the supported shape ----------------------------------------------------------------

def test_supported_shape_runs_for_real_with_progress_and_enriched_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    a, b = _build_case(db_path)
    request = _request(tmp_path, db_path)

    progress_calls: list[tuple[int, int]] = []
    result = run_concomitance(
        request, on_progress=lambda done, total: progress_calls.append((done, total)))

    assert result.match_count == 1
    assert result.candidate_count_1 == 2
    assert result.candidate_count_2 == 2
    assert result.output_path == request.output_path
    # Both groups are empty ("everything"), which resolves to the SAME selection in one
    # case, so the identical-groups WARNING fires (see check_request) but does not block.
    assert len(result.warnings) == 1
    assert progress_calls == [(1, 1)]

    raw = request.output_path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("g1_identifier,g1_identifier_type,g1_entity_type")
    assert len(lines) == 2
    row = lines[1].split(",")
    # g1/g2 identifiers are the two fixture devices; lat/lon/time ARE populated (absent
    # from the raw engine ConcomitantPair, enriched here).
    assert {row[0], row[10]} == {"+41790000001", "+41790000002"}
    assert {row[8], row[18]} == {a, b}
    latitudes = sorted((float(row[3]), float(row[13])))
    expected = sorted((KNOWN[0], _north_of(10.0)[0]))
    assert latitudes == pytest.approx(expected, abs=1e-3)
    assert row[6] and row[7]           # g1_time_start / g1_time_end are non-empty ISO
    assert row[16] and row[17]         # g2_time_start / g2_time_end
    assert row[9] and row[19]          # g1_source / g2_source (traceability)


def test_identical_groups_warning_is_surfaced_but_the_run_proceeds(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    group = EntityGroup()   # empty == empty: same_selection is True
    request = _request(tmp_path, db_path, group_1=group, group_2=group)
    result = run_concomitance(request)
    assert result.match_count == 1
    assert len(result.warnings) == 1
    assert "same entities in one case" in result.warnings[0].message


def test_invalid_request_raises_value_error(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path, space_threshold_m=-1.0)
    with pytest.raises(ValueError, match="Distance threshold"):
        run_concomitance(request)


def test_cancellation_stops_early_keeps_partial_file(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path)
    result = run_concomitance(request, should_cancel=lambda: True)
    assert result.match_count == 0
    assert request.output_path.is_file()   # the sink is closed cleanly, never deleted
    raw = request.output_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert len(raw.decode("utf-8-sig").splitlines()) == 1   # header only
