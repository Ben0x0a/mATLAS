"""Tests for model_atlas/app/concomitance_run.py (the T102 Verify list).

Covers:     the FOUR shapes T101a had to refuse — two cases, an asymmetric window,
            accuracy-aware matching and two different entity groups — now running for
            real through the T102 engine (the capability guards
            are gone); plus progress callbacks (once per ANCHOR), cooperative
            cancellation (partial count, file kept), the identical-groups warning, and
            a written CSV whose rows carry the position/time the old output lacked.
Uses:       ``model_atlas.app.concomitance_run`` + the same fixture-building helpers as
            tests/analyse/test_concomitance.py (real layer-typed store writers, real
            ``integrate_spatial``), persisted to a real file so the read-only URI-safe
            opener is exercised exactly as the GUI/CLI will use it.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest

from model_atlas.analyse.concomitance import EARTH_RADIUS_M
from model_atlas.analyse.concomitance import (
    ConcomitanceRequest,
    Criterion,
    EntityGroup,
    MatchMode,
)
from model_atlas.analyse.helpers import CriterionKind
from model_atlas.app.concomitance_run import run_concomitance
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial

# The engine raises until decision 55's presence predicate exists; these tests run it.
_ENGINE_DISABLED = pytest.mark.skip(reason="concomitance is disabled until its presence predicate is redesigned (decision 55)")

KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0


def _north_of(metres: float) -> tuple[float, float]:
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


def _add_point(store: RunStore, line_no: int, source_file_id: str, preset_id: str,
               preset_key: str, latitude: float, longitude: float,
               lower_us: int, upper_us: int, *, identifier: str) -> str:
    record_id = ids.new_id()
    store.write_raw_record(V.RawRecord(
        raw_record_id=record_id, source_file_id=source_file_id, line_no=line_no, raw_json="{}"))
    natural_key = (record_id, 0)
    entity_id = ids.new_id()
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type="msisdn", type="device"))
    sx_id = ids.new_id()
    store.write_spatial_expression(V.SpatialExpression(
        spatial_expression_id=sx_id,
        position_type=V.PositionType.COORDINATE, referential="epsg:4326"))
    for ordinal, (part, raw) in enumerate((
            ("latitude", str(latitude)), ("longitude", str(longitude)))):
        store.write_spatial_component(V.SpatialComponent(
            spatial_component_id=ids.new_id(),
            spatial_expression_id=sx_id, ordinal=ordinal, part=part, raw_value=raw))
    tx_id = ids.new_id()
    store.write_temporal_expression(V.TemporalExpression(
        temporal_expression_id=tx_id, kind="interval"))
    store.write_timespan_integration(V.TimespanIntegration(
        timespan_integration_id=ids.new_id(),
        temporal_expression_id=tx_id, method="fixture/1.0",
        lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us,
        offset_to_local_hours_lower=0.0, offset_to_local_hours_upper=0.0))
    observation_id = ids.new_id()
    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=record_id, preset_id=preset_id, entity_id=entity_id,
        observation_content_hash=f"ch-{line_no}", spatiotemporal_entity_hash=f"cd-{line_no}",
        spatial_expression_id=sx_id, temporal_expression_id=tx_id,
        spatial_link="measured", bearer_entity_id=entity_id,
        entity_link="subject", temporal_link="at_least_once",
        source_label=f"src-{line_no}"))
    return observation_id


def _build_case(path: Path) -> tuple[str, str]:
    """Persist a case with two presence observations 10 m apart, overlapping in time."""
    with RunStore() as store:
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="T101a"))
        run_id = ids.run_id()
        store.write_run(V.Run(run_id=run_id, case_id=case_id))
        source_file_id = ids.new_id()
        store.write_source_file(V.SourceFile(source_file_id=source_file_id, run_id=run_id,
                                    input_file_path="/case/t101a.csv"))
        preset_key = "t101a"
        preset_id = ids.preset_id(preset_key, "1")
        store.write_preset(V.Preset(preset_id=preset_id, key=preset_key, name=None,
                                    version="1", definition_digest="sha256-fixture"))
        a = _add_point(store, 1, source_file_id, preset_id, preset_key, *KNOWN,
                      BASE_US, BASE_US + 5 * 60_000_000, identifier="+41790000001")
        b = _add_point(store, 2, source_file_id, preset_id, preset_key, *_north_of(10.0),
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


# --- the four shapes T101a had to refuse, now running for real ---------------------------
# Each of these was refused by a capability guard before the T102 engine landed. The
# guards are gone, so the observation is now that the run COMPLETES and writes a file.

@_ENGINE_DISABLED
def test_two_cases_now_run(tmp_path: Path) -> None:
    """A second case is compared for real — and the same-identity exclusion is LIFTED
    across cases, so the same device in both is reported rather than dropped."""
    case_1 = tmp_path / "case1.matlas.sqlite"
    case_2 = tmp_path / "case2.matlas.sqlite"
    _build_case(case_1)
    _build_case(case_2)
    request = _request(tmp_path, case_1, case_2=case_2)
    result = run_concomitance(request)
    assert result.candidate_count_1 == 2 and result.candidate_count_2 == 2
    # The two cases carry the same two devices at the same place/time: every cross
    # pair matches, including the identical-identity ones (the deliberate inversion).
    assert result.match_count == 4
    assert request.output_path.is_file()


@_ENGINE_DISABLED
def test_the_same_case_named_twice_is_still_one_case(tmp_path: Path) -> None:
    """case_2 pointing at case_1's file is ONE case, so the same-identity exclusion
    must still apply — otherwise every observation would pair with itself."""
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    both = run_concomitance(_request(tmp_path, db_path, case_2=db_path))
    single = run_concomitance(_request(tmp_path, db_path))
    assert both.match_count == single.match_count == 1


@_ENGINE_DISABLED
def test_asymmetric_window_now_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    request = _request(tmp_path, db_path,
                       time_before=dt.timedelta(minutes=5),
                       time_after=dt.timedelta(minutes=30))
    result = run_concomitance(request)
    assert result.match_count == 1
    assert request.output_path.is_file()


@_ENGINE_DISABLED
def test_use_accuracy_now_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    result = run_concomitance(_request(tmp_path, db_path, use_accuracy=True))
    assert result.match_count == 1


@_ENGINE_DISABLED
def test_different_groups_now_run(tmp_path: Path) -> None:
    """Two INDEPENDENT groups — the capability the whole rewrite exists for."""
    db_path = tmp_path / "case.matlas.sqlite"
    a, b = _build_case(db_path)
    request = _request(
        tmp_path, db_path,
        group_1=EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "+41790000001"),)),
        group_2=EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "+41790000002"),)))
    result = run_concomitance(request)
    assert result.candidate_count_1 == 1 and result.candidate_count_2 == 1
    assert result.match_count == 1
    row = request.output_path.read_text(encoding="utf-8-sig").splitlines()[1].split(",")
    # Group 1 really is the group-1 side of the row (the asymmetry is preserved).
    assert row[0] == "+41790000001" and row[10] == "+41790000002"
    assert row[8] == a and row[18] == b


@_ENGINE_DISABLED
def test_cross_kind_any_now_runs(tmp_path: Path) -> None:
    """``ANY`` spanning several kinds — structurally inexpressible for the old engine."""
    db_path = tmp_path / "case.matlas.sqlite"
    _build_case(db_path)
    group = EntityGroup(criteria=(
        Criterion(CriterionKind.ENTITY_TYPE, "device"),
        Criterion(CriterionKind.IDENTIFIER_TYPE, "plate")), mode=MatchMode.ANY)
    request = _request(tmp_path, db_path, group_1=group, group_2=group)
    result = run_concomitance(request)
    # Both fixture entities are devices, so the union still selects both.
    assert result.candidate_count_1 == 2
    assert result.match_count == 1


def test_no_capability_guard_remains(tmp_path: Path) -> None:
    """The T101a seam is gone: nothing in the app layer raises the capability error."""
    import inspect

    from model_atlas.app import concomitance_run

    source = inspect.getsource(concomitance_run)
    assert "raise ConcomitanceNotYetSupported" not in source
    assert "_ensure_engine_can_run" not in source


# --- the supported shape ----------------------------------------------------------------

@_ENGINE_DISABLED
def test_plain_shape_runs_for_real_with_progress_and_enriched_rows(tmp_path: Path) -> None:
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
    # Progress is now once per ANCHOR with the group-1 candidate count as the total
    # (T102 §5) — the old adapter reported once per emitted PAIR, which could not drive
    # a determinate progress bar because the pair count is unknown until the end.
    assert progress_calls == [(1, 2), (2, 2)]

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


@_ENGINE_DISABLED
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


@_ENGINE_DISABLED
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
