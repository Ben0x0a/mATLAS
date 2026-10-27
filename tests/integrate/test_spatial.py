"""Tests for model_atlas/integrate/spatial.py (the S2 spatial-index Verify list).

Covers:     the known-point leaf cell + level, the uint64<->signed-int64 storage
            round-trip (including a real top-bit-set point), the ancestor-sharing /
            range-containment property the concomitance engine relies on, the m/s->km/h
            and ft->m unit conversions, the method stamp on every integration row, and
            the no-coordinates -> no spatial_index row rule.
Uses:       ``model_atlas.integrate.spatial``, ``model_atlas.core`` (ids,
            vocabularies, store) and ``s2sphere`` directly for the reference values.
Note:       every read-back goes through plain ``sqlite3`` (the store connection) —
            pandas' ``read_sql_query`` would lift a nullable INTEGER into float64,
            whose 53-bit mantissa silently corrupts int64 S2 cell ids.
"""
from __future__ import annotations

import pytest
import s2sphere

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import (
    S2_LEAF_LEVEL,
    CellRegisterLookup,
    GeocodeAddress,
    DecodeGridReference,
    PassthroughWgs84,
    SpatialExpressionInput,
    UnitConvert,
    integrate_expression,
    integrate_spatial,
    s2_cell_from_stored,
    s2_cell_to_stored,
    s2_leaf_cell_id,
)

# Reference points. KNOWN's leaf id was computed once with s2sphere 0.2.5 and pinned:
# a drift here would mean the library (or our call into it) changed the index bits,
# which would desynchronise every existing run DB — exactly what the pin must catch.
KNOWN = (46.2030, 6.1430)
KNOWN_LEAF_ID = 5155606902436257935          # uint64 == signed (top bit clear, face 2)
NEAR = (46.2030 + 10.0 / 111_320.0, 6.1430)  # ~10 m north of KNOWN
FAR = (48.8566, 2.3522)                       # Paris, ~400 km away
SOUTH_POLE = (-90.0, 0.0)                     # face 5 -> top bit SET (signed negative)
SOUTH_POLE_LEAF_ID = 12682136550675316737


def _leaf(lat: float, lon: float) -> s2sphere.CellId:
    return s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(lat, lon))


# --- the S2 leaf cell + the signed storage helpers --------------------------

def test_known_point_maps_to_expected_leaf_cell() -> None:
    cell = _leaf(*KNOWN)
    assert cell.id() == KNOWN_LEAF_ID
    assert cell.level() == S2_LEAF_LEVEL == 30
    # The build-time helper returns the STORED (signed) form of the same leaf.
    assert s2_leaf_cell_id(*KNOWN) == s2_cell_to_stored(KNOWN_LEAF_ID)


def test_signed_int64_round_trip() -> None:
    # Edge values around the sign boundary plus both ends of the uint64 range: the
    # stored form must stay within SQLite's signed 64-bit INTEGER and invert exactly.
    for value in (0, 1, 2**62, 2**63 - 1, 2**63, 2**63 + 1, 2**64 - 1):
        stored = s2_cell_to_stored(value)
        assert -(2**63) <= stored < 2**63
        assert s2_cell_from_stored(stored) == value
    # Top bit set -> negative; clear -> unchanged.
    assert s2_cell_to_stored(2**63) == -(2**63)
    assert s2_cell_to_stored(2**63 - 1) == 2**63 - 1


def test_real_top_bit_set_point_round_trips_negative() -> None:
    # A face-5 point (southern skies) exercises the negative branch with a REAL cell.
    assert _leaf(*SOUTH_POLE).id() == SOUTH_POLE_LEAF_ID
    stored = s2_leaf_cell_id(*SOUTH_POLE)
    assert stored < 0
    assert s2_cell_from_stored(stored) == SOUTH_POLE_LEAF_ID


def test_close_points_share_low_level_ancestor_far_points_do_not() -> None:
    known, near, far = _leaf(*KNOWN), _leaf(*NEAR), _leaf(*FAR)
    # ~10 m apart: one level-12 (~3-5 km) ancestor cell; ~400 km apart: not even that.
    ancestor = known.parent(12)
    assert near.parent(12) == ancestor
    assert far.parent(12) != ancestor
    # The property T41's BETWEEN scans rely on: in STORED form, the near leaf falls
    # inside the ancestor's [range_min, range_max] and the far leaf falls outside —
    # the signed reinterpretation preserves order because a cell range never crosses
    # a face (top-bit) boundary.
    low = s2_cell_to_stored(ancestor.range_min().id())
    high = s2_cell_to_stored(ancestor.range_max().id())
    assert low <= s2_cell_to_stored(near.id()) <= high
    assert not low <= s2_cell_to_stored(far.id()) <= high


# --- L1 fixtures -------------------------------------------------------------

def _write_expression(store: RunStore, tag: str, referential: str | None,
                      parts: list[tuple[str, str | None]],
                      position_type: str = V.PositionType.COORDINATE.value) -> str:
    """Write one spatial_expression + its verbatim components; return its id."""
    expression_id = ids.new_id()
    store.write_spatial_expression(V.SpatialExpression(
        spatial_expression_id=expression_id,
        position_type=position_type, referential=referential))
    for ordinal, (part, raw_value) in enumerate(parts):
        store.write_spatial_component(V.SpatialComponent(
            spatial_component_id=ids.new_id(),
            spatial_expression_id=expression_id,
            ordinal=ordinal,
            part=part,
            raw_value=raw_value,
        ))
    return expression_id


# --- idempotency / append ----------------------------------------------------

def test_integrate_spatial_is_idempotent_and_integrates_new_only() -> None:
    # A re-run (or an append adding runs to a case) integrates only NOT-yet-integrated
    # expressions and never collides on an existing spatial_integration row.
    with RunStore() as store:
        _write_expression(store, "a", "epsg:4326",
                          [("latitude", "46.20"), ("longitude", "6.14")])
        assert integrate_spatial(store).integration_count == 1
        assert integrate_spatial(store).integration_count == 0   # nothing new, no collision
        _write_expression(store, "b", "epsg:4326",
                          [("latitude", "47.00"), ("longitude", "7.00")])
        assert integrate_spatial(store).integration_count == 1   # only the new one
        total = store.connection.execute(
            "SELECT COUNT(*) FROM spatial_integration").fetchone()[0]
        assert total == 2


# --- passthrough + unit conversions -------------------------------------------

def test_passthrough_and_unit_conversions_write_one_stamped_row() -> None:
    with RunStore() as store:
        expression_id = _write_expression(store, "gnss", "epsg:4326", [
            ("latitude", "46.2030"),
            ("longitude", "6.1430"),
            ("altitude_m", "100"),            # source unit ft -> 30.48 m
            ("horizontal_speed_kmh", "10"),   # source unit m/s -> 36.0 km/h
            ("heading_deg", "220"),           # already canonical (identity factor)
        ])
        report = integrate_spatial(store, source_units={
            "altitude_m": "ft", "horizontal_speed_kmh": "m/s"})
        assert report.integration_count == 1
        assert report.index_count == 1

        row = store.connection.execute(
            "SELECT spatial_integration_id, method, latitude_wgs84, longitude_wgs84,"
            "       altitude_m, horizontal_speed_kmh, heading_deg"
            "  FROM spatial_integration WHERE spatial_expression_id = ?",
            (expression_id,)).fetchone()
        integration_id, method, lat, lon, alt, speed, heading = row
        # The stored method string names every applied rule, in method-set order.
        assert method == "passthrough_wgs84/1.0+unit_convert/1.0"
        assert integration_id            # random (decision 50); one row per expression
                                         # is what UNIQUE(spatial_expression_id) enforces
        assert (lat, lon) == (46.2030, 6.1430)
        assert alt == pytest.approx(30.48)     # 100 ft * 0.3048
        assert speed == pytest.approx(36.0)    # 10 m/s * 3.6
        assert heading == pytest.approx(220.0)

        # D31: the point gets its one leaf index row, in signed storage form.
        cell_id, level = store.connection.execute(
            "SELECT s2_cell_id, s2_level FROM spatial_index "
            "WHERE spatial_integration_id = ?", (integration_id,)).fetchone()
        assert cell_id == s2_cell_to_stored(KNOWN_LEAF_ID)
        assert level == S2_LEAF_LEVEL


def test_unknown_source_unit_raises_loudly() -> None:
    expression = SpatialExpressionInput("sx-test", "unknown", {"horizontal_speed_kmh": "10"}, position_type="coordinate")
    with pytest.raises(ValueError):
        UnitConvert({"horizontal_speed_kmh": "furlong/fortnight"}).integrate(expression)


def test_non_numeric_raw_value_raises_loudly() -> None:
    expression = SpatialExpressionInput("sx-test", "epsg:4326", {"latitude": "pres de la gare"}, position_type="coordinate")
    with pytest.raises(ValueError):
        PassthroughWgs84().integrate(expression)


@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "nan"])
def test_non_finite_metric_raw_value_raises_not_bogus_magnitude(raw: str) -> None:
    # Some devices emit 'inf' for an unknown speed/accuracy: bare float() would parse it
    # and silently stamp horizontal_speed_kmh = inf as evidence. It must raise instead.
    expression = SpatialExpressionInput("sx-test", "unknown", {"horizontal_speed_kmh": raw}, position_type="coordinate")
    with pytest.raises(ValueError):
        UnitConvert().integrate(expression)


@pytest.mark.parametrize("raw", ["nan", "inf"])
def test_non_finite_coordinate_raises_before_s2sphere(raw: str) -> None:
    # 'nan'/'inf' under a coordinate must raise the documented ValueError HERE, not crash
    # deep in s2sphere.from_lat_lng (an AssertionError that would abort the whole pass).
    expression = SpatialExpressionInput("sx-test", "epsg:4326", {"latitude": raw, "longitude": "6.14"}, position_type="coordinate")
    with pytest.raises(ValueError):
        PassthroughWgs84().integrate(expression)


def test_finite_float_still_parses() -> None:
    # The guard rejects only non-finite values — an ordinary coordinate is untouched.
    expression = SpatialExpressionInput(
        "sx-test", "epsg:4326",
        {"latitude": "46.2030", "longitude": "6.1430"})
    # The datum-agnostic L1 parts ``latitude`` / ``longitude`` are MAPPED to the canonical
    # L2 columns ``latitude_wgs84`` / ``longitude_wgs84`` at the point of WGS84 adoption.
    assert PassthroughWgs84().integrate(expression) == {
        "latitude_wgs84": 46.2030, "longitude_wgs84": 6.1430}


# --- every row has a method; no coordinates -> no index row --------------------

def test_every_integration_row_is_method_stamped() -> None:
    with RunStore() as store:
        _write_expression(store, "a", "epsg:4326",
                          [("latitude", "46.2030"), ("longitude", "6.1430")])
        _write_expression(store, "b", "unknown",
                          [("horizontal_speed_kmh", "50")])
        integrate_spatial(store)
        methods = [m for (m,) in store.connection.execute(
            "SELECT method FROM spatial_integration")]
        assert len(methods) == 2
        assert all(m for m in methods)   # never NULL, never empty (L2 invariant)
        # The metric-only row is stamped by the unit interpretation alone.
        assert "unit_convert/1.0" in methods


def test_no_coordinates_means_no_index_row() -> None:
    with RunStore() as store:
        # An address not yet geocoded: NO method applies -> no integration, no index.
        _write_expression(store, "addr", None,
                          [("raw_position", "12 rue de la Gare, Geneve")],
                          position_type=V.PositionType.STREET_ADDRESS.value)
        # A metric-only expression: an integration WITHOUT coordinates -> no index.
        speed_expression = _write_expression(store, "speed", "unknown",
                                             [("vertical_speed_kmh", "12")])
        report = integrate_spatial(store)
        assert report.integration_count == 1
        assert report.index_count == 0
        rows = store.connection.execute(
            "SELECT spatial_expression_id FROM spatial_integration").fetchall()
        assert rows == [(speed_expression,)]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM spatial_index").fetchone() == (0,)


# --- the composer + the stubs ---------------------------------------------------

def test_composer_returns_none_when_nothing_applies() -> None:
    expression = SpatialExpressionInput("sx-test", "address", {"raw_position": "x"})
    assert integrate_expression(
        expression, (PassthroughWgs84(), UnitConvert())) is None


def test_stubs_recognise_their_position_types_but_refuse_to_integrate() -> None:
    """Dispatch is on the TYPE, not the referential — the point of the split.

    A BSSID is a MAC address with no referential to match on, so a mechanism that dispatched
    on the referential could never be added for it without inventing a fake one.
    """
    cases = [
        (GeocodeAddress(), "street_address"),
        (DecodeGridReference(), "grid_reference"),
        (CellRegisterLookup(), "cell_id"),
    ]
    for method, position_type in cases:
        expression = SpatialExpressionInput(
            "sx-test", None, position_type=position_type)
        assert method.applies_to(expression)
        assert not method.applies_to(SpatialExpressionInput(
            "sx-test", "epsg:4326", position_type="coordinate"))
        with pytest.raises(NotImplementedError):
            method.integrate(expression)


def test_duplicate_interpretable_part_raises() -> None:
    with RunStore() as store:
        _write_expression(store, "dup", "epsg:4326", [
            ("latitude", "46.0"),
            ("latitude", "47.0"),   # two candidates for ONE canonical column
            ("longitude", "6.0"),
        ])
        with pytest.raises(ValueError):
            integrate_spatial(store)


# --- position_raw: the evidence behind a derived coordinate --------------------

def test_position_raw_records_the_reprojection_inputs() -> None:
    """A reprojection reads the two angle parts, so those are what it evidences."""
    expression = SpatialExpressionInput(
        spatial_expression_id="sx-repro", referential=V.SpatialReferential.EPSG_4326.value,
        position_type=V.PositionType.COORDINATE.value,
        part_values={"latitude": "46.193000", "longitude": "6.129000",
                     "altitude_m": "375"},
        part_source_fields={"latitude": "ZLATITUDE", "longitude": "ZLONGITUDE",
                            "altitude_m": "ZALTITUDE"})
    row = integrate_expression(
        expression, [PassthroughWgs84(), UnitConvert()])
    assert row.position_raw == "ZLATITUDE:46.193000 | ZLONGITUDE:6.129000"
    # The altitude was converted in the same row but had NO part in producing the
    # coordinate, so it is deliberately absent from the evidence.
    assert "ZALTITUDE" not in row.position_raw


def test_position_raw_follows_the_mechanism_not_the_angle_parts() -> None:
    """The readiness guard for geocoding / cell / BSSID lookups.

    ``position_raw`` is scoped to the information that ALLOWED the computation, not to the
    latitude and longitude components — the two coincide only while reprojection is the
    sole mechanism. A method producing the same two canonical columns from an ADDRESS must
    evidence the address, with no change to the composer. If this test ever has to be
    edited to make a new mechanism pass, the scope has silently narrowed back.
    """
    class _FakeGeocoder:
        name, version = "geocode_address", "test"

        def applies_to(self, expression):
            return expression.position_type == V.PositionType.STREET_ADDRESS.value

        def integrate(self, expression):
            return {"latitude_wgs84": 46.2030, "longitude_wgs84": 6.1400}

        def coordinate_inputs(self, expression):
            return [p for p in ("raw_position",) if p in expression.part_values]

    expression = SpatialExpressionInput(
        spatial_expression_id="sx-geo", referential=None,
        position_type=V.PositionType.STREET_ADDRESS.value,
        part_values={"raw_position": "Rue du Stand 15, Geneve", "altitude_m": "375"},
        part_source_fields={"raw_position": "ZSTREET", "altitude_m": "ZALTITUDE"})
    row = integrate_expression(
        expression, [_FakeGeocoder(), UnitConvert()])
    assert row.latitude_wgs84 == 46.2030
    assert row.position_raw == "ZSTREET:Rue du Stand 15, Geneve"


def test_position_raw_absent_when_no_coordinate_was_produced() -> None:
    """Metrics alone evidence nothing: position_raw exists to back a COORDINATE."""
    expression = SpatialExpressionInput(
        spatial_expression_id="sx-metrics", referential=V.SpatialReferential.UNKNOWN.value,
        position_type=V.PositionType.COORDINATE.value,
        part_values={"altitude_m": "375"}, part_source_fields={"altitude_m": "ZALTITUDE"})
    row = integrate_expression(expression, [UnitConvert()])
    assert row is not None and row.altitude_m == 375.0
    assert row.position_raw is None


def test_expression_input_refuses_a_fourth_positional_argument() -> None:
    """A new field can never silently rebind ``part_values``.

    The three-argument positional form is used throughout this file, so any field added
    after ``part_values`` would, if it were positional, swallow the parts dict: the Method
    would then see an expression with NO components and quietly produce nothing — turning
    the "must raise" tests below into passes. That is a green suite hiding a broken guard,
    and it happened twice before the fields were made keyword-only.

    A comment cannot enforce an ordering constraint. This test pins the enforcement.
    """
    expression = SpatialExpressionInput("sx", "epsg:4326", {"latitude": "46.2"}, position_type="coordinate")
    assert expression.part_values == {"latitude": "46.2"}
    assert expression.part_source_fields == {}   # not rebound by the positional dict

    with pytest.raises(TypeError):
        SpatialExpressionInput(          # type: ignore[misc]
            "sx", "epsg:4326", {"latitude": "46.2"}, {"latitude": "ZLATITUDE"})
