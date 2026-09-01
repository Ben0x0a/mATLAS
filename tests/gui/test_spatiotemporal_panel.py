"""End-to-end panel test for the rewritten Spatiotemporal Analysis controller (T101a).

Drives the real SpatiotemporalController offscreen against a persisted run DB: opening a
case populates the group dropdowns from the case vocabulary; adding/removing criteria
updates the group's chip list; a criterion kind absent from the case yields an EMPTY
dropdown; an invalid request blocks Run with a message; the run itself goes off-thread
via TaskRunner and writes a CSV via model_atlas.app.concomitance_run, never a results
table (there is none any more, and no Visualise button).

Uses:       gui.spatiotemporal_controller, model_atlas.core (ids/vocabularies/store),
            model_atlas.integrate.spatial (real S2 index build), PySide6 (offscreen).
"""
from __future__ import annotations

import os

# Offscreen BEFORE any Qt import so the suite runs headless (matches test_tab_shell).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import datetime as dt
import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from gui.spatiotemporal_controller import SpatiotemporalController, _SIDE_1, _SIDE_2
from model_atlas.analyse.concomitance import EARTH_RADIUS_M
from model_atlas.analyse.helpers import CriterionKind
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial
from tests.gui.conftest import drain

KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0


def _north_of(metres: float) -> tuple[float, float]:
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


def _add_point(store: RunStore, line_no: int, source_id: str, preset_id: str,
               preset_key: str, latitude: float, longitude: float,
               lower_us: int, upper_us: int, *, identifier: str,
               identifier_type: str = "msisdn", entity_type: str = "device") -> str:
    record_id = ids.raw_record_id(source_id, line_no)
    store.write_raw_record(V.RawRecord(
        raw_record_id=record_id, source_id=source_id, line_no=line_no, raw_json="{}"))
    natural_key = (record_id, 0)
    entity_id = ids.entity_id(natural_key)
    store.write_entity(V.Entity(
        entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
        identifier_type=identifier_type, type=entity_type))
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
    observation_id = ids.observation_id(record_id, preset_key, 0)
    store.write_observation(V.Observation(
        observation_id=observation_id, raw_record_id=record_id, preset_id=preset_id,
        output_ordinal=0, entity_id=entity_id,
        content_hash=f"ch-{line_no}", claim_digest=f"cd-{line_no}",
        spatial_expression_id=sx_id, temporal_expression_id=tx_id,
        entity_role="subject", entity_position_link="at",
        entity_time_link="observed_at", spatial_temporal_link="instant"))
    return observation_id


def _build_persisted_db(path: Path) -> tuple[str, str]:
    """Persist a small run DB: two presence device observations 10 m apart, overlapping."""
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


def _open_case_1(app: QApplication, controller: SpatiotemporalController,
                 db_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(db_path), "")))
    controller.case_1_button.click()
    app.processEvents()


def _run_and_wait(app: QApplication, controller: SpatiotemporalController) -> None:
    controller.run_button.click()
    controller._runner.wait_all()
    app.processEvents()


def _teardown(app: QApplication, controller: SpatiotemporalController,
              parent: QWidget) -> None:
    """Shut the controller down BEFORE its widgets die.

    A standalone panel owns its own TaskRunner, so deleting the parent without joining it
    leaves a worker that can finish into freed widgets — the intermittent teardown segfault
    this suite has a history of. Mirrors the shared controller fixture in conftest.py."""
    controller.shutdown()
    drain(app)
    # Only the parent is deleted: the controller is PARENTED to it, so Qt destroys it as a
    # child. (conftest's MainController is unparented, which is why it needs its own
    # deleteLater; copying that here would be a redundant double-delete.)
    parent.deleteLater()
    drain(app)


def test_opening_a_case_populates_the_group_dropdowns(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_persisted_db(db_path)

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        _open_case_1(app, controller, db_path, monkeypatch)
        assert controller.case_1_edit.text() == str(db_path)

        # Group 1 defaults to "Entity type": the case has one entity type, 'device'.
        widgets_1 = controller._groups[_SIDE_1]
        assert widgets_1.kind_combo.currentIndex() == 0
        assert widgets_1.value_combo.count() == 1
        assert widgets_1.value_combo.itemData(0) == "device"

        # Group 2 (unticked "compare against a second case") ALSO reads Case 1's
        # vocabulary, since both groups are drawn from case_1 by default.
        widgets_2 = controller._groups[_SIDE_2]
        assert widgets_2.value_combo.count() == 1
        assert widgets_2.value_combo.itemData(0) == "device"

        # Switching Group 1's kind to Identifier shows the two device identifiers.
        widgets_1.kind_combo.setCurrentIndex(2)   # "Identifier"
        values = {widgets_1.value_combo.itemData(i) for i in range(widgets_1.value_combo.count())}
        assert values == {"+41790000001", "+41790000002"}
    finally:
        _teardown(app, controller, parent)


def test_criterion_kind_absent_from_the_case_yields_an_empty_dropdown(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_persisted_db(db_path)   # no 'vehicle'/'plate' entities at all

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        _open_case_1(app, controller, db_path, monkeypatch)
        widgets = controller._groups[_SIDE_1]
        widgets.kind_combo.setCurrentIndex(1)   # "Identifier type": only 'msisdn' present
        values = {widgets.value_combo.itemData(i) for i in range(widgets.value_combo.count())}
        assert values == {"msisdn"}
        # An identifier_type that never occurs in this case is simply not offered, and
        # the widget never falls back to the full static vocabulary.
        assert "plate" not in values
    finally:
        _teardown(app, controller, parent)


def test_adding_and_removing_criteria_updates_the_group(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_persisted_db(db_path)

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        _open_case_1(app, controller, db_path, monkeypatch)
        widgets = controller._groups[_SIDE_1]
        assert widgets.criteria == []
        # The empty-state placeholder is the list's own single non-interactive row.
        assert controller._groups[_SIDE_1].chips_list.count() == 1
        assert controller._groups[_SIDE_1].chips_list.item(0).text() == (
            "All entities in this case")

        widgets.kind_combo.setCurrentIndex(0)   # Entity type
        widgets.value_combo.setCurrentIndex(0)  # the only entry: 'device'
        controller._add_criterion(_SIDE_1)
        assert [c.value for c in widgets.criteria] == ["device"]
        assert widgets.criteria[0].kind is CriterionKind.ENTITY_TYPE
        assert controller._groups[_SIDE_1].chips_list.count() == 1
        assert controller._groups[_SIDE_1].chips_list.item(0).text() != (
            "All entities in this case")

        controller._remove_criterion(_SIDE_1, widgets.criteria[0])
        assert widgets.criteria == []
        assert controller._groups[_SIDE_1].chips_list.item(0).text() == (
            "All entities in this case")
    finally:
        _teardown(app, controller, parent)


def test_run_on_an_invalid_request_is_blocked_with_a_message(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    _build_persisted_db(db_path)

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        _open_case_1(app, controller, db_path, monkeypatch)
        controller.space_spin.setValue(0.0)
        controller.time_before_spin.setValue(0.0)
        controller.time_after_spin.setValue(0.0)

        shown: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: shown.append(a[2]) or QMessageBox.StandardButton.Ok))

        controller.run_button.click()
        app.processEvents()

        assert shown
        assert "both zero matches nothing" in shown[0]
        assert not controller._running
    finally:
        _teardown(app, controller, parent)


def test_the_results_table_and_visualise_button_are_gone(
        app: QApplication, tmp_path) -> None:
    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        assert not hasattr(controller, "results_table")
        assert not hasattr(controller, "_model")
        assert not hasattr(controller, "visualise_button")
        assert not hasattr(controller, "export_button")
    finally:
        _teardown(app, controller, parent)


def test_full_run_writes_a_csv_with_the_expected_pair(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    a, b = _build_persisted_db(db_path)

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        _open_case_1(app, controller, db_path, monkeypatch)
        controller.space_spin.setValue(50.0)
        controller.time_before_spin.setValue(10.0)
        controller.time_after_spin.setValue(10.0)
        controller.time_before_unit_combo.setCurrentText("minutes")
        controller.time_after_unit_combo.setCurrentText("minutes")
        out_csv = tmp_path / "out.csv"
        controller.output_edit.setText(str(out_csv))

        # Both groups are empty ("everything"): check_request warns (identical groups in
        # one case) but does not block; accept the Continue prompt.
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

        _run_and_wait(app, controller)

        assert controller._last_result is not None
        assert controller._last_result.match_count == 1
        assert out_csv.is_file()
        lines = out_csv.read_bytes().decode("utf-8-sig").splitlines()
        assert lines[0].startswith("g1_identifier,g1_identifier_type,g1_entity_type")
        assert len(lines) == 2
        assert controller.open_output_folder_button.isEnabled()
    finally:
        _teardown(app, controller, parent)
