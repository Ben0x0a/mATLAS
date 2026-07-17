"""End-to-end panel test for the Spatiotemporal Analysis controller (T72).

Drives the real SpatiotemporalController offscreen against a persisted run DB: the
query runs OFF-THREAD via TaskRunner, so the test waits on the runner and drains the
queued result signal (the gui.app tab-shell pattern). Checks the Verify list:
selecting entities within both thresholds lists the expected concomitant pair, a
``references`` pin is NOT listed (the presence split), and Export writes a CSV. A
second test drives the shipped T00 demo DB read-only when it is present.

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

from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from gui.spatiotemporal_controller import SpatiotemporalController, open_persisted_run_view
from model_atlas.analyze.concomitance import EARTH_RADIUS_M
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial

# A demo-flavoured scene; a pure-latitude offset of d/M_PER_DEG_LAT degrees IS d
# metres on the engine's sphere (meridian arcs are exact great circles).
KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0

DEMO_DB = Path(__file__).resolve().parents[2] / "docs" / "examples" / "matlas_example.sqlite"


def _north_of(metres: float) -> tuple[float, float]:
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _add_point(store: RunStore, line_no: int, source_id: str, preset_id: str,
               preset_key: str, latitude: float, longitude: float,
               lower_us: int, upper_us: int, *, identifier: str,
               position_link: str = "at") -> str:
    """Write one placed-and-timed presence (or referenced) assertion; return its id.

    Mirrors tests/analyze/test_concomitance.py's fixture so the persisted DB the panel
    queries is built through the REAL layer-typed store writers.
    """
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
            ("latitude_wgs84", str(latitude)), ("longitude_wgs84", str(longitude)))):
        store.write_spatial_component(V.SpatialComponent(
            spatial_component_id=ids.spatial_component_id(sx_id, ordinal),
            spatial_expression_id=sx_id, ordinal=ordinal, part=part, raw_value=raw))
    tx_id = ids.temporal_expression_id(natural_key)
    store.write_temporal_expression(V.TemporalExpression(
        temporal_expression_id=tx_id, kind="interval"))
    store.write_timespan_integration(V.TimespanIntegration(
        timespan_integration_id=ids.timespan_integration_id(tx_id, "fixture/1.0"),
        temporal_expression_id=tx_id, method="fixture/1.0",
        lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us))
    assertion_id = ids.assertion_id(record_id, preset_key, 0)
    store.write_assertion(V.Assertion(
        assertion_id=assertion_id, raw_record_id=record_id, preset_id=preset_id,
        output_ordinal=0, entity_id=entity_id,
        content_hash=f"ch-{line_no}", claim_digest=f"cd-{line_no}",
        spatial_expression_id=sx_id, temporal_expression_id=tx_id,
        entity_role="subject", entity_position_link=position_link,
        entity_time_link="observed_at", spatial_temporal_link="instant"))
    return assertion_id


def _build_persisted_db(path: Path) -> tuple[str, str, str]:
    """Persist a small run DB: two presence entities that pair + one references pin.

    Returns (assertion_a, assertion_b, references_assertion) — the two ids that must
    pair and the pin id that must be excluded.
    """
    with RunStore() as store:
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id, case_number="T72"))
        run_id = ids.run_id()
        store.write_run(V.Run(run_id=run_id, case_id=case_id))
        source_id = ids.source_id("/case/t72.csv", None, None)
        store.write_source(V.Source(source_id=source_id, run_id=run_id,
                                    input_file_path="/case/t72.csv"))
        preset_key = "t72"
        preset_id = ids.preset_id(preset_key, "1")
        store.write_preset(V.Preset(preset_id=preset_id, key=preset_key, name=None,
                                    version="1", definition_digest="sha256-fixture"))
        a = _add_point(store, 1, source_id, preset_id, preset_key, *KNOWN,
                       BASE_US, BASE_US + 5 * 60_000_000, identifier="+41790000001")
        b = _add_point(store, 2, source_id, preset_id, preset_key, *_north_of(10.0),
                       BASE_US + 2 * 60_000_000, BASE_US + 6 * 60_000_000,
                       identifier="+41790000002")
        # A pin REFERRING to the same place/time: never presence, must be excluded.
        pin = _add_point(store, 3, source_id, preset_id, preset_key, *KNOWN,
                         BASE_US, BASE_US + 5 * 60_000_000,
                         identifier="+41790000003", position_link="references")
        integrate_spatial(store)
        store.persist(path)
    return a, b, pin


def _run_and_wait(app: QApplication, controller: SpatiotemporalController) -> None:
    """Trigger the query and drain the off-thread result back onto the GUI thread."""
    controller.run_button.click()
    controller._runner.wait_all()
    app.processEvents()


def _teardown(app: QApplication, parent: QWidget) -> None:
    parent.deleteLater()
    app.processEvents()


def test_panel_lists_expected_pair_excludes_references_and_exports(
        app: QApplication, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "case.matlas.sqlite"
    assertion_a, assertion_b, pin = _build_persisted_db(db_path)

    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        controller.db_edit.setText(str(db_path))
        controller.space_spin.setValue(50.0)
        controller.time_spin.setValue(10.0)
        controller.time_unit_combo.setCurrentText("minutes")
        _run_and_wait(app, controller)

        # Exactly the one presence pair (10 m apart, overlapping time) is listed.
        assert controller._result is not None
        assert controller._result.candidate_count == 2   # the pin is not a candidate
        assert controller._model.rowCount() == 1
        keys = {(p.assertion_a, p.assertion_b) for p in controller._result.pairs}
        assert keys == {(min(assertion_a, assertion_b), max(assertion_a, assertion_b))}
        # The references pin appears in NO pair (the presence split).
        paired = {pair.assertion_a for pair in controller._result.pairs}
        paired |= {pair.assertion_b for pair in controller._result.pairs}
        assert pin not in paired

        # Export is enabled and writes a real CSV (via the T41 to_csv helper).
        assert controller.export_button.isEnabled()
        out_csv = tmp_path / "out.csv"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(out_csv), "")))
        controller.export_button.click()
        app.processEvents()
        assert out_csv.exists()
        raw = out_csv.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")   # utf-8-sig BOM (house rule)
        lines = raw.decode("utf-8-sig").splitlines()
        assert lines[0].startswith("entity_a_id,entity_a_identifier")
        assert len(lines) == 1 + len(controller._result.pairs) == 2
    finally:
        _teardown(app, parent)


def test_read_only_view_never_mutates_the_persisted_db(tmp_path) -> None:
    db_path = tmp_path / "ro.matlas.sqlite"
    _build_persisted_db(db_path)
    view = open_persisted_run_view(db_path)
    try:
        # A read is fine; any write is rejected by the mode=ro connection.
        assert view.connection.execute("SELECT COUNT(*) FROM assertion").fetchone()[0] > 0
        with pytest.raises(Exception):
            view.connection.execute(
                "INSERT INTO case_metadata (case_id, case_number) VALUES ('x','y')")
    finally:
        view.close()


@pytest.mark.skipif(not DEMO_DB.is_file(), reason="T00 demo DB not present")
def test_panel_drives_the_demo_db_read_only(app: QApplication, tmp_path,
                                            monkeypatch) -> None:
    parent = QWidget()
    controller = SpatiotemporalController(parent)
    try:
        controller.db_edit.setText(str(DEMO_DB))
        controller.space_spin.setValue(300.0)
        controller.time_spin.setValue(30.0)
        controller.time_unit_combo.setCurrentText("minutes")
        _run_and_wait(app, controller)

        assert controller._result is not None
        assert controller._result.pairs                     # the demo yields pairs
        assert controller._model.rowCount() == len(controller._result.pairs)
        # No references assertion is ever listed (the presence split), on the real DB.
        references = {
            row[0] for row in view_all_references(DEMO_DB)}
        paired = {p.assertion_a for p in controller._result.pairs}
        paired |= {p.assertion_b for p in controller._result.pairs}
        assert not (references & paired)

        assert controller.export_button.isEnabled()
        out_csv = tmp_path / "demo.csv"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(out_csv), "")))
        controller.export_button.click()
        app.processEvents()
        assert out_csv.exists()
    finally:
        _teardown(app, parent)


def view_all_references(db_path: Path) -> list[tuple[str]]:
    view = open_persisted_run_view(db_path)
    try:
        return view.connection.execute(
            "SELECT assertion_id FROM assertion "
            "WHERE entity_position_link = 'references'").fetchall()
    finally:
        view.close()
