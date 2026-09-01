"""Tests for model_atlas/analyse/concomitance.py (the T102 Verify list).

Covers:     BRUTE-FORCE PARITY as the primary oracle — the engine's streamed matches
            must equal a direct O(n²) evaluation of the stated definition, for the
            symmetric no-accuracy case, for two distinct groups, for an asymmetric
            window and with accuracy on; the MIN_WIDTH level choice being a genuine
            superset (and AVG_EDGE demonstrably not); the motivating one-identifier
            case; the asymmetric window and its signed time_delta_s; overlapping
            groups emitting each pair exactly once; the two-sided accuracy case; the
            split-accuracy-axes counterexample (a named regression test); a randomised
            no-loss property sweep; strategy equivalence + a reported benchmark; two
            stores with the inverted same-identity rule; cancellation; and progress.
Uses:       ``model_atlas.analyse.concomitance``, ``model_atlas.analyse.
            concomitance.request`` / ``concomitance.output`` (the agreed contracts),
            ``model_atlas.core`` (ids, vocabularies, store) and ``model_atlas.integrate.
            spatial`` — the fixture writes L1 rows and builds the S2 index through the
            REAL ``integrate_spatial`` driver, so query-vs-build bit-parity is
            exercised end to end.

The oracle rule (task T102): correctness is agreement with brute force, NEVER with the
previous implementation, which was never validated against real casework.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import time

import pytest
import s2sphere

from model_atlas.analyse.concomitance import (
    Criterion,
    EntityGroup,
    MatchMode,
    MatchRow,
)
from model_atlas.analyse.concomitance.engine import (
    EARTH_RADIUS_M,
    SpatialStrategy,
    find_concomitant,
    great_circle_distance_m,
    s2_level_for_radius,
)
from model_atlas.analyse.helpers import CriterionKind
from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import integrate_spatial

# The reference point (same as tests/integrate/test_spatial.py) and time origin.
KNOWN = (46.2030, 6.1430)
BASE_US = 1_773_453_600_000_000            # 2026-03-14T02:00:00Z in unix µs
# Metres per degree of latitude on the engine's sphere: meridian arcs are exact
# great circles, so a pure-latitude offset of d/M_PER_DEG_LAT degrees IS d metres.
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0

MINUTE_US = 60_000_000


def _minutes(count: float) -> int:
    """Unix µs ``count`` minutes after the BASE_US origin (exact for the ints used)."""
    return BASE_US + int(count * MINUTE_US)


def _north_of(metres: float) -> tuple[float, float]:
    """The point ``metres`` due north of KNOWN (an exact great-circle offset)."""
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


class _CaseFixture:
    """L0 scaffolding + an ``add_point`` writer for one candidate observation.

    Writes the full FK chain (case -> run -> source/preset -> raw_record ->
    entity/expressions -> observation) through the layer-typed store writers; the caller
    runs ``integrate_spatial`` once afterwards so the spatial_integration and
    spatial_index rows — including ``horizontal_accuracy_m`` — come from the REAL build
    path.
    """

    def __init__(self, store: RunStore, case_number: str = "T102-CASE") -> None:
        self.store = store
        self.line_no = 0
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id,
                                                 case_number=case_number))
        run_id_ = ids.run_id()
        store.write_run(V.Run(run_id=run_id_, case_id=case_id))
        self.source_id = ids.source_id(f"/case/{case_number}.csv", None, None)
        store.write_source(V.Source(source_id=self.source_id, run_id=run_id_,
                                    input_file_path=f"/case/{case_number}.csv"))
        self.preset_key = f"fixture_{case_number}"
        self.preset_id = ids.preset_id(self.preset_key, "1")
        store.write_preset(V.Preset(preset_id=self.preset_id, key=self.preset_key,
                                    name=None, version="1",
                                    definition_digest="sha256-fixture"))

    def add_point(self, latitude: float, longitude: float,
                  lower_us: int, upper_us: int, *,
                  identifier: str | None,
                  identifier_type: str = "msisdn",
                  entity_type: str = "device",
                  accuracy_m: float | None = None,
                  lower_accuracy_us: int | None = None,
                  upper_accuracy_us: int | None = None,
                  position_link: str = "at",
                  time_link: str = "observed_at",
                  st_link: str = "instant") -> str:
        """One placed-and-timed observation; returns its observation_id."""
        self.line_no += 1
        record_id = ids.raw_record_id(self.source_id, self.line_no)
        self.store.write_raw_record(V.RawRecord(
            raw_record_id=record_id, source_id=self.source_id,
            line_no=self.line_no, raw_json="{}"))
        natural_key = (record_id, 0)
        entity_id = ids.entity_id(natural_key)
        self.store.write_entity(V.Entity(
            entity_id=entity_id, identifier=identifier, raw_identifier=identifier,
            identifier_type=identifier_type, type=entity_type))
        sx_id = ids.spatial_expression_id(natural_key)
        self.store.write_spatial_expression(V.SpatialExpression(
            spatial_expression_id=sx_id, referential="epsg:4326"))
        parts: list[tuple[str, str]] = [("latitude", str(latitude)),
                                        ("longitude", str(longitude))]
        if accuracy_m is not None:
            # A metric part: UnitConvert carries it into spatial_integration
            # .horizontal_accuracy_m, so the engine reads a REALLY built column.
            parts.append(("horizontal_accuracy_m", str(accuracy_m)))
        for ordinal, (part, raw) in enumerate(parts):
            self.store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.spatial_component_id(sx_id, ordinal),
                spatial_expression_id=sx_id, ordinal=ordinal,
                part=part, raw_value=raw))
        tx_id = ids.temporal_expression_id(natural_key)
        self.store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind="interval"))
        self.store.write_timespan_integration(V.TimespanIntegration(
            timespan_integration_id=ids.timespan_integration_id(tx_id, "fixture/1.0"),
            temporal_expression_id=tx_id, method="fixture/1.0",
            lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us,
            lower_accuracy_us=lower_accuracy_us,
            upper_accuracy_us=upper_accuracy_us,
            lower_datetime_iso=dt.datetime.fromtimestamp(
                lower_us / 1_000_000, tz=dt.timezone.utc).isoformat(),
            upper_datetime_iso=dt.datetime.fromtimestamp(
                upper_us / 1_000_000, tz=dt.timezone.utc).isoformat()))
        observation_id = ids.observation_id(record_id, self.preset_key, 0)
        self.store.write_observation(V.Observation(
            observation_id=observation_id, raw_record_id=record_id,
            preset_id=self.preset_id, output_ordinal=0, entity_id=entity_id,
            content_hash=f"ch-{self.line_no}", claim_digest=f"cd-{self.line_no}",
            spatial_expression_id=sx_id, temporal_expression_id=tx_id,
            entity_role="subject", entity_position_link=position_link,
            entity_time_link=time_link, spatial_temporal_link=st_link,
            source_label=f"src-{self.line_no}"))
        return observation_id


class _CollectingSink:
    """A ``MatchSink`` that keeps the rows in order, so a test can assert on them.

    The engine streams; this is the smallest possible thing that satisfies the
    protocol, so the tests exercise the REAL streaming path rather than a variant.
    """

    def __init__(self) -> None:
        self.rows: list[MatchRow] = []
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def write(self, match: MatchRow) -> None:
        self.rows.append(match)

    def close(self) -> None:
        self.closed = True


def _run(store_1, store_2=None, *, group_1=None, group_2=None,
         space_threshold_m: float = 50.0,
         time_before: dt.timedelta = dt.timedelta(minutes=10),
         time_after: dt.timedelta = dt.timedelta(minutes=10),
         **kwargs):
    """Run the engine into a collecting sink; return (summary, rows)."""
    sink = _CollectingSink()
    summary = find_concomitant(
        store_1, store_2,
        group_1=group_1 if group_1 is not None else EntityGroup(),
        group_2=group_2 if group_2 is not None else EntityGroup(),
        space_threshold_m=space_threshold_m,
        time_before=time_before, time_after=time_after,
        sink=sink, **kwargs)
    return summary, sink.rows


def _pair_keys(rows: list[MatchRow]) -> set[frozenset[str]]:
    """The UNORDERED observation pairs a result carries ("a match is a match")."""
    return {frozenset((r.g1_observation_id, r.g2_observation_id)) for r in rows}


# --- the brute-force oracle -------------------------------------------------------
# Deliberately written out longhand — its candidate SQL, its group predicate and its
# window arithmetic are NOT imported from the engine, so a bug there cannot hide inside
# the parity. Only great_circle_distance_m is shared: the parity target is the
# candidate-PAIRING path (region choice + range scans + dedup), not the distance
# formula, so sharing the formula keeps the comparison exact.

_BRUTE_CANDIDATE_SQL = """
SELECT a.observation_id, e.identifier, e.identifier_type, e.type,
       si.latitude_wgs84, si.longitude_wgs84, si.horizontal_accuracy_m,
       ti.lower_unix_utc_us, ti.upper_unix_utc_us,
       ti.lower_accuracy_us, ti.upper_accuracy_us
FROM observation a
JOIN entity e ON e.entity_id = a.entity_id
JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id
JOIN spatial_index six ON six.spatial_integration_id = si.spatial_integration_id
JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id
WHERE ti.lower_unix_utc_us IS NOT NULL AND ti.upper_unix_utc_us IS NOT NULL
  AND si.latitude_wgs84 IS NOT NULL AND si.longitude_wgs84 IS NOT NULL
  AND (a.entity_position_link IS NULL OR a.entity_position_link != 'references')
  AND (a.entity_time_link IS NULL
       OR a.entity_time_link != 'never_during_interval')
  AND (a.spatial_temporal_link IS NULL
       OR a.spatial_temporal_link != 'never_during_interval')
"""


class _Row:
    """One brute-force candidate, with the accuracies already NULL-collapsed to 0."""

    def __init__(self, raw) -> None:
        (self.observation_id, self.identifier, self.identifier_type, self.entity_type,
         self.latitude, self.longitude, accuracy_m,
         self.lower_us, self.upper_us, lower_acc, upper_acc) = raw
        self.accuracy_m = accuracy_m or 0.0
        self.lower_acc_us = lower_acc or 0
        self.upper_acc_us = upper_acc or 0


def _brute_candidates(store) -> list[_Row]:
    return [_Row(r) for r in store.connection.execute(_BRUTE_CANDIDATE_SQL).fetchall()]


def _in_group(row: _Row, group: EntityGroup) -> bool:
    """Whether one candidate satisfies a group — the definition, re-stated in Python."""
    if not group.criteria:
        return True                       # empty group = everything (narrowing opt-in)
    column = {CriterionKind.ENTITY_TYPE: row.entity_type,
              CriterionKind.IDENTIFIER_TYPE: row.identifier_type,
              CriterionKind.IDENTIFIER: row.identifier}
    per_kind: list[bool] = []
    for kind in CriterionKind:
        values = [c.value for c in group.criteria if c.kind is kind]
        if values:
            per_kind.append(column[kind] in values)
    return any(per_kind) if group.mode is MatchMode.ANY else all(per_kind)


def _brute_window_ok(a: _Row, b: _Row, before_us: int, after_us: int,
                     use_accuracy: bool) -> bool:
    """Is ``b`` inside ``a``'s directional window? (a is the group-1 side.)"""
    if b.lower_us > a.upper_us:           # b is LATER
        slack = a.upper_acc_us + b.lower_acc_us if use_accuracy else 0
        return (b.lower_us - a.upper_us) - slack <= after_us
    if a.lower_us > b.upper_us:           # b is EARLIER
        slack = a.lower_acc_us + b.upper_acc_us if use_accuracy else 0
        return (a.lower_us - b.upper_us) - slack <= before_us
    return True                           # overlapping intervals


def _brute_force_pairs(store_1, store_2=None, *,
                       group_1: EntityGroup = EntityGroup(),
                       group_2: EntityGroup = EntityGroup(),
                       space_threshold_m: float = 50.0,
                       time_before: dt.timedelta = dt.timedelta(minutes=10),
                       time_after: dt.timedelta = dt.timedelta(minutes=10),
                       use_accuracy: bool = False) -> set[frozenset[str]]:
    """Every true match as an UNORDERED observation pair — the definition, O(n²).

    One store applies the same-identity exclusion; two stores lift it (the deliberate
    inversion). A pair counts once however many orientations satisfy the window.
    """
    one_store = store_2 is None or store_2 is store_1
    rows_1 = [r for r in _brute_candidates(store_1) if _in_group(r, group_1)]
    rows_2 = [r for r in _brute_candidates(store_2 if not one_store else store_1)
              if _in_group(r, group_2)]
    before_us = time_before // dt.timedelta(microseconds=1)
    after_us = time_after // dt.timedelta(microseconds=1)

    pairs: set[frozenset[str]] = set()
    for a in rows_1:
        for b in rows_2:
            if one_store:
                if a.observation_id == b.observation_id:
                    continue
                if (a.identifier is not None and a.identifier == b.identifier
                        and a.identifier_type == b.identifier_type):
                    continue              # convergence, not concomitance
            reach = space_threshold_m + (
                a.accuracy_m + b.accuracy_m if use_accuracy else 0.0)
            if great_circle_distance_m(a.latitude, a.longitude,
                                       b.latitude, b.longitude) > reach:
                continue
            if not _brute_window_ok(a, b, before_us, after_us, use_accuracy):
                continue
            pairs.add(frozenset((a.observation_id, b.observation_id)))
    return pairs


# The kwargs that define the QUESTION (and so belong to the oracle too); anything else
# passed to _run — strategy, max_cells, callbacks — tunes the engine, never the answer.
_DEFINITION_KWARGS = frozenset({
    "group_1", "group_2", "space_threshold_m", "time_before", "time_after",
    "use_accuracy"})


def _assert_parity(store_1, store_2=None, **kwargs) -> list[MatchRow]:
    """Engine == brute force, and each true pair emitted EXACTLY once."""
    summary, rows = _run(store_1, store_2, **kwargs)
    expected = _brute_force_pairs(store_1, store_2, **{
        k: v for k, v in kwargs.items() if k in _DEFINITION_KWARGS})
    assert _pair_keys(rows) == expected
    # Once-ness: no pair may be emitted twice (no seen-set exists to catch it).
    assert len(rows) == len(expected)
    assert summary.match_count == len(rows)
    return rows


# --- 1 · the S2 level choice is a genuine superset (and AVG_EDGE is not) ------------

def _destination(lat: float, lon: float, distance_m: float, bearing_deg: float):
    """The point ``distance_m`` from (lat, lon) on ``bearing_deg`` — exact on the
    engine's sphere, so the test's geometry and the engine's agree by construction."""
    phi_1, lambda_1 = math.radians(lat), math.radians(lon)
    delta, theta = distance_m / EARTH_RADIUS_M, math.radians(bearing_deg)
    phi_2 = math.asin(math.sin(phi_1) * math.cos(delta)
                      + math.cos(phi_1) * math.sin(delta) * math.cos(theta))
    lambda_2 = lambda_1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi_1),
        math.cos(delta) - math.sin(phi_1) * math.sin(phi_2))
    return math.degrees(phi_2), math.degrees(lambda_2)


def _block_at(lat: float, lon: float, level: int) -> set[int]:
    """The anchor's cell + its neighbours at ``level``, as raw cell ids."""
    leaf = s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(lat, lon))
    parent = leaf.parent(level)
    return {parent.id()} | {c.id() for c in parent.get_all_neighbors(level)}


# The sweep size the design was measured at, and a seed that exhibits the AVG_EDGE
# trap. Other seeds (1, 2026) show ZERO AVG_EDGE misses at the same size — which is
# precisely why this defect would survive casual testing and has to be pinned.
_SUPERSET_SAMPLES = 6000
_SUPERSET_SEED = 777


def _anchor_partner_samples(radius_m: float):
    """Random anchor/partner pairs at most ``radius_m`` apart, uniform on the sphere.

    Shared by BOTH metric checks so they are compared on the IDENTICAL points — the
    difference between them is then the metric alone, not the sample.
    """
    rng = random.Random(_SUPERSET_SEED)
    for _ in range(_SUPERSET_SAMPLES):
        # asin of a uniform draw = uniform by AREA, so no latitude band is
        # over-sampled (a naive uniform latitude would crowd the poles).
        lat = math.degrees(math.asin(rng.uniform(-1.0, 1.0)))
        lon = rng.uniform(-180.0, 180.0)
        yield (lat, lon), _destination(lat, lon, rng.uniform(0.0, radius_m),
                                       rng.uniform(0.0, 360.0))


def _misses_for_metric(metric, radius_m: float) -> int:
    """How many points within ``radius_m`` of an anchor fall OUTSIDE the anchor's cell
    and neighbours at the level this metric chooses. Zero is the superset property."""
    level = next(L for L in range(30, -1, -1)
                 if metric.get_value(L) * EARTH_RADIUS_M >= radius_m)
    misses = 0
    for (lat, lon), (p_lat, p_lon) in _anchor_partner_samples(radius_m):
        partner = s2sphere.CellId.from_lat_lng(
            s2sphere.LatLng.from_degrees(p_lat, p_lon)).parent(level)
        if partner.id() not in _block_at(lat, lon, level):
            misses += 1
    return misses


@pytest.mark.parametrize("radius_m", (100.0, 1000.0, 5000.0))
def test_min_width_level_block_is_a_superset(radius_m: float) -> None:
    """The property the whole strategy rests on: nothing within r escapes the block."""
    assert _misses_for_metric(s2sphere.MIN_WIDTH, radius_m) == 0
    # ...and the engine's chooser really is the MIN_WIDTH one.
    expected = next(L for L in range(30, -1, -1)
                    if s2sphere.MIN_WIDTH.get_value(L) * EARTH_RADIUS_M >= radius_m)
    assert s2_level_for_radius(radius_m) == expected


def test_avg_edge_would_ship_this_broken() -> None:
    """The silent-loss trap, pinned: the SAME sweep built on AVG_EDGE fails at 1 000 m.

    AVG_EDGE is an AVERAGE, so a below-average cell at the chosen level is narrower
    than r and a true partner falls outside the block. On these identical points
    MIN_WIDTH misses nothing while AVG_EDGE misses several — and it misses nothing at
    100 m or 5 000 m, which is exactly why casual testing would not catch it.
    """
    assert _misses_for_metric(s2sphere.AVG_EDGE, 1000.0) > 0
    assert _misses_for_metric(s2sphere.MIN_WIDTH, 1000.0) == 0
    # The radii where the average happens to be safe — the reason this is a trap.
    assert _misses_for_metric(s2sphere.AVG_EDGE, 100.0) == 0
    assert _misses_for_metric(s2sphere.AVG_EDGE, 5000.0) == 0


def test_radius_wider_than_a_face_drops_the_spatial_restriction() -> None:
    """No level is wide enough, so the engine must scan everything rather than
    under-cover (over-selecting is always allowed; missing never is)."""
    assert s2_level_for_radius(20_000_000.0) is None
    assert s2_level_for_radius(0.0) == 30


# --- 2 · brute-force parity on the seeded random fixture ----------------------------

def _random_fixture(store, *, count: int = 60, seed: int = 41,
                    with_accuracy: bool = False) -> None:
    """Mixed points in a ~2 km box over a 2 h window; 7 recurring identities so the
    same-identity exclusion is exercised INSIDE the parity, every 8th row non-presence
    so the exclusion filter is exercised on both paths, and three entity types so the
    group predicates have something to select on."""
    rng = random.Random(seed)
    fixture = _CaseFixture(store)
    links = ("at", "at", "at", "at", "at", "at", "at", "references")
    types = ("device", "vehicle", "camera")
    id_types = ("msisdn", "plate", "camera")
    for i in range(count):
        latitude = KNOWN[0] + rng.uniform(-0.010, 0.010)
        longitude = KNOWN[1] + rng.uniform(-0.015, 0.015)
        lower = _minutes(rng.uniform(0.0, 120.0))
        upper = lower + int(rng.uniform(0.0, 10.0) * MINUTE_US)
        kind = i % 3
        fixture.add_point(
            latitude, longitude, lower, upper,
            identifier=f"+4179000000{i % 7}",
            identifier_type=id_types[kind], entity_type=types[kind],
            accuracy_m=(rng.choice((None, 20.0, 120.0)) if with_accuracy else None),
            lower_accuracy_us=(rng.choice((None, 30 * 1_000_000)) if with_accuracy
                               else None),
            upper_accuracy_us=(rng.choice((None, 90 * 1_000_000)) if with_accuracy
                               else None),
            position_link=links[i % len(links)])
    integrate_spatial(store)


def test_parity_symmetric_no_accuracy() -> None:
    """The closest analogue of the OLD engine's question — and the oracle it never had."""
    with RunStore() as store:
        _random_fixture(store)
        rows = _assert_parity(store, space_threshold_m=150.0,
                              time_before=dt.timedelta(minutes=15),
                              time_after=dt.timedelta(minutes=15))
        assert rows, "the fixture must actually produce matches"


def test_parity_two_distinct_groups() -> None:
    """Two INDEPENDENT groups — the capability the old symmetric selector lacked."""
    with RunStore() as store:
        _random_fixture(store)
        _assert_parity(
            store,
            group_1=EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE,
                                                    "device"),)),
            group_2=EntityGroup(criteria=(Criterion(CriterionKind.ENTITY_TYPE,
                                                    "vehicle"),)),
            space_threshold_m=400.0,
            time_before=dt.timedelta(minutes=20),
            time_after=dt.timedelta(minutes=20))


def test_parity_asymmetric_window() -> None:
    with RunStore() as store:
        _random_fixture(store)
        _assert_parity(store, space_threshold_m=200.0,
                       time_before=dt.timedelta(minutes=3),
                       time_after=dt.timedelta(minutes=40))


def test_parity_with_accuracy() -> None:
    with RunStore() as store:
        _random_fixture(store, with_accuracy=True)
        _assert_parity(store, space_threshold_m=120.0,
                       time_before=dt.timedelta(minutes=5),
                       time_after=dt.timedelta(minutes=25),
                       use_accuracy=True)


def test_parity_any_across_kinds_and_all_within_a_kind() -> None:
    """The two MatchMode shapes the T101 adapter could not express at all."""
    with RunStore() as store:
        _random_fixture(store)
        _assert_parity(
            store,
            group_1=EntityGroup(
                criteria=(Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),
                          Criterion(CriterionKind.IDENTIFIER_TYPE, "camera")),
                mode=MatchMode.ANY),
            group_2=EntityGroup(
                criteria=(Criterion(CriterionKind.ENTITY_TYPE, "device"),
                          Criterion(CriterionKind.IDENTIFIER_TYPE, "msisdn")),
                mode=MatchMode.ALL),
            space_threshold_m=300.0,
            time_before=dt.timedelta(minutes=10),
            time_after=dt.timedelta(minutes=10))


# --- 3 · the motivating case --------------------------------------------------------

def _three_entity_fixture(store) -> tuple[str, str, str]:
    fixture = _CaseFixture(store)
    a = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001")
    b = fixture.add_point(*_north_of(10.0), _minutes(1), _minutes(6),
                          identifier="+41790000002")
    c = fixture.add_point(*_north_of(20.0), _minutes(2), _minutes(7),
                          identifier="+41790000003")
    integrate_spatial(store)
    return a, b, c


def test_one_identifier_against_everything_finds_its_partners() -> None:
    """THE motivating case: "who was with this phone". Today's engine returned 0.

    One identifier on the group-1 side and an empty group 2 must yield exactly the
    pairs that identifier participates in — and the partners must be the other two.
    """
    with RunStore() as store:
        a, b, c = _three_entity_fixture(store)
        group_1 = EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "+41790000001"),))
        rows = _assert_parity(store, group_1=group_1, group_2=EntityGroup(),
                              space_threshold_m=50.0,
                              time_before=dt.timedelta(minutes=10),
                              time_after=dt.timedelta(minutes=10))
        assert len(rows) == 2
        assert _pair_keys(rows) == {frozenset((a, b)), frozenset((a, c))}
        # The named entity is always the group-1 side of the emitted row.
        assert {r.g1_identifier for r in rows} == {"+41790000001"}
        assert {r.g2_identifier for r in rows} == {"+41790000002", "+41790000003"}


def test_a_lower_sorting_partner_is_not_dropped() -> None:
    """The regression an unconditional ``observation_id > anchor`` filter would cause.

    With DISJOINT groups only one visit exists, so suppressing it by id order loses the
    match outright. Asserted in BOTH id directions so the test cannot pass by luck.
    """
    with RunStore() as store:
        fixture = _CaseFixture(store)
        device = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                                   identifier="+41790000001")
        vehicle = fixture.add_point(*_north_of(10.0), _minutes(1), _minutes(6),
                                    identifier="GE123456",
                                    identifier_type="plate", entity_type="vehicle")
        integrate_spatial(store)
        devices = EntityGroup(criteria=(
            Criterion(CriterionKind.ENTITY_TYPE, "device"),))
        vehicles = EntityGroup(criteria=(
            Criterion(CriterionKind.ENTITY_TYPE, "vehicle"),))
        for group_1, group_2 in ((devices, vehicles), (vehicles, devices)):
            _, rows = _run(store, group_1=group_1, group_2=group_2)
            assert _pair_keys(rows) == {frozenset((device, vehicle))}, (
                "a disjoint-group pair was dropped by id order")


# --- 4 · the directional window -----------------------------------------------------

def test_asymmetric_window_direction_and_signed_delta() -> None:
    """A partner 20 min AFTER the anchor matches with after=30m/before=10m and not with
    after=10m/before=30m; the signed time_delta_s carries the direction."""
    with RunStore() as store:
        fixture = _CaseFixture(store)
        anchor = fixture.add_point(*KNOWN, _minutes(0), _minutes(0),
                                   identifier="+41790000001")
        fixture.add_point(*_north_of(10.0), _minutes(20), _minutes(20),
                          identifier="+41790000002")
        integrate_spatial(store)
        group_1 = EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "+41790000001"),))

        _, wide = _run(store, group_1=group_1,
                       time_after=dt.timedelta(minutes=30),
                       time_before=dt.timedelta(minutes=10))
        assert len(wide) == 1
        assert wide[0].g1_observation_id == anchor
        assert wide[0].time_delta_s == pytest.approx(20 * 60.0)   # POSITIVE = later
        assert wide[0].time_overlap is False

        _, narrow = _run(store, group_1=group_1,
                         time_after=dt.timedelta(minutes=10),
                         time_before=dt.timedelta(minutes=30))
        assert narrow == []

        # Mirror it: the same pair anchored from the LATER side reads negative.
        later = EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "+41790000002"),))
        _, mirrored = _run(store, group_1=later,
                           time_after=dt.timedelta(minutes=10),
                           time_before=dt.timedelta(minutes=30))
        assert len(mirrored) == 1
        assert mirrored[0].time_delta_s == pytest.approx(-20 * 60.0)


def test_overlapping_groups_emit_each_pair_once_including_one_way_windows() -> None:
    """Both groups empty: each pair appears exactly once, and a pair that satisfies the
    window in only ONE orientation is still emitted (a match is a match)."""
    with RunStore() as store:
        fixture = _CaseFixture(store)
        early = fixture.add_point(*KNOWN, _minutes(0), _minutes(0),
                                  identifier="+41790000001")
        late = fixture.add_point(*_north_of(10.0), _minutes(20), _minutes(20),
                                 identifier="+41790000002")
        integrate_spatial(store)
        # after=30m/before=0: satisfied only when the EARLY point is the anchor.
        _, rows = _run(store, time_after=dt.timedelta(minutes=30),
                       time_before=dt.timedelta(0))
        assert len(rows) == 1
        assert _pair_keys(rows) == {frozenset((early, late))}
        # Whichever orientation the engine chose, it must be a satisfying one.
        assert rows[0].time_delta_s == pytest.approx(20 * 60.0)


# --- 5 · accuracy -------------------------------------------------------------------

def test_accuracy_turns_a_near_miss_into_a_match() -> None:
    """150 m apart, 100 m stated accuracy each, 50 m threshold: matches only with
    use_accuracy, and distance_within_accuracy says accuracy was what did it."""
    with RunStore() as store:
        fixture = _CaseFixture(store)
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001", accuracy_m=100.0)
        fixture.add_point(*_north_of(150.0), _minutes(0), _minutes(5),
                          identifier="+41790000002", accuracy_m=100.0)
        integrate_spatial(store)

        _, off = _run(store, space_threshold_m=50.0)
        assert off == []
        _, on = _run(store, space_threshold_m=50.0, use_accuracy=True)
        assert len(on) == 1
        assert on[0].distance_within_accuracy is True
        assert on[0].distance_m == pytest.approx(150.0, abs=0.1)
        assert on[0].g1_accuracy_m == 100.0 and on[0].g2_accuracy_m == 100.0

        # A pair inside the threshold on its own reports False for the same flag.
        _, plain = _run(store, space_threshold_m=200.0, use_accuracy=True)
        assert plain[0].distance_within_accuracy is False


def test_two_sided_accuracy_is_reported_exactly_once() -> None:
    """The scenario that motivated the responsibility rule: a 5 km-accuracy point and a
    10 km-accuracy point 12 km apart is a true match (12 <= 0 + 5000 + 10000... in
    metres: 12 000 <= 15 000). It must be reported EXACTLY once."""
    with RunStore() as store:
        fixture = _CaseFixture(store)
        a = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                              identifier="+41790000001", accuracy_m=5000.0)
        b = fixture.add_point(*_north_of(12_000.0), _minutes(0), _minutes(5),
                              identifier="+41790000002", accuracy_m=10_000.0)
        integrate_spatial(store)

        summary, rows = _run(store, space_threshold_m=0.0, use_accuracy=True,
                             time_before=dt.timedelta(minutes=10),
                             time_after=dt.timedelta(minutes=10))
        assert _pair_keys(rows) == {frozenset((a, b))}
        assert len(rows) == 1                      # once-ness, not twice
        assert summary.match_count == 1
        # The FUZZIER point is the responsible anchor — that is what makes the single
        # visit the one that can actually reach.
        assert rows[0].g1_observation_id == b
        assert rows[0].g1_accuracy_m == 10_000.0


def test_split_accuracy_axes_counterexample_is_found() -> None:
    """NAMED REGRESSION TEST — the case a per-point rule on BOTH axes silently loses.

    Threshold and window both 0. Point a is fuzzy in space only (10 m), point b in time
    only (10 s), at d = 8 m and gap = 8 s. This IS a true match (8 <= 0+10+0 on space
    and 8 <= 0+0+10 on time), but anchoring at a reaches spatially and not temporally,
    and anchoring at b the reverse — so a per-point temporal rule finds it from
    NEITHER side. The engine must widen the temporal axis by the global maximum.
    """
    with RunStore() as store:
        fixture = _CaseFixture(store)
        a = fixture.add_point(*KNOWN, _minutes(0), _minutes(0),
                              identifier="+41790000001", accuracy_m=10.0)
        b = fixture.add_point(*_north_of(8.0), BASE_US + 8_000_000,
                              BASE_US + 8_000_000,
                              identifier="+41790000002",
                              lower_accuracy_us=10_000_000,
                              upper_accuracy_us=10_000_000)
        integrate_spatial(store)

        _, rows = _run(store, space_threshold_m=0.0, use_accuracy=True,
                       time_before=dt.timedelta(0), time_after=dt.timedelta(0))
        assert _pair_keys(rows) == {frozenset((a, b))}, (
            "the split-accuracy-axes match was lost — the temporal axis must widen "
            "by the GLOBAL maximum partner accuracy, not a per-point value")
        assert len(rows) == 1


def test_a_fuzzier_partner_in_a_disjoint_group_is_still_found() -> None:
    """The correction this engine makes to the per-point spatial rule.

    Groups are DISJOINT, so only the group-1 side ever anchors. The group-2 partner is
    the fuzzier one, so it never gets its turn to reach back — a per-anchor radius of
    ``threshold + 2*acc(anchor)`` would miss this true match entirely.
    """
    with RunStore() as store:
        fixture = _CaseFixture(store)
        device = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                                   identifier="+41790000001", accuracy_m=5000.0)
        tower = fixture.add_point(*_north_of(12_000.0), _minutes(0), _minutes(5),
                                  identifier="ANT-1", identifier_type="cell_id",
                                  entity_type="antenna", accuracy_m=10_000.0)
        integrate_spatial(store)

        _, rows = _run(
            store,
            group_1=EntityGroup(criteria=(
                Criterion(CriterionKind.ENTITY_TYPE, "device"),)),
            group_2=EntityGroup(criteria=(
                Criterion(CriterionKind.ENTITY_TYPE, "antenna"),)),
            space_threshold_m=0.0, use_accuracy=True)
        assert _pair_keys(rows) == {frozenset((device, tower))}, (
            "a fuzzier group-2 partner was unreachable: with disjoint groups the "
            "partner term must be the GLOBAL maximum, not the anchor's own accuracy")


def test_randomised_no_loss_property_with_mixed_accuracies() -> None:
    """The sweep that would have caught the split-axis bug: over a few thousand
    generated pairs with mixed spatial AND temporal accuracies, every true match is
    emitted exactly once. Brute force is the oracle throughout."""
    rng = random.Random(2026)
    with RunStore() as store:
        fixture = _CaseFixture(store)
        for i in range(90):
            # A tight box so many pairs are genuinely near, and accuracies that put
            # the fuzz on DIFFERENT points (the split-axis shape) about half the time.
            latitude = KNOWN[0] + rng.uniform(-0.004, 0.004)
            longitude = KNOWN[1] + rng.uniform(-0.006, 0.006)
            lower = _minutes(rng.uniform(0.0, 60.0))
            upper = lower + int(rng.uniform(0.0, 3.0) * MINUTE_US)
            spatial_fuzz = rng.choice((None, None, 50.0, 250.0))
            temporal_fuzz = rng.choice((None, None, 45 * 1_000_000, 200 * 1_000_000))
            fixture.add_point(
                latitude, longitude, lower, upper,
                identifier=f"+417900000{i:02d}",
                accuracy_m=spatial_fuzz,
                lower_accuracy_us=temporal_fuzz, upper_accuracy_us=temporal_fuzz)
        integrate_spatial(store)

        # 90 candidates => 4 005 unordered pairs evaluated by the oracle.
        rows = _assert_parity(store, space_threshold_m=100.0,
                              time_before=dt.timedelta(minutes=2),
                              time_after=dt.timedelta(minutes=6),
                              use_accuracy=True)
        assert len(rows) > 50, "the sweep must actually exercise real matches"


# --- 6 · strategy equivalence + the reported benchmark --------------------------------

def test_both_strategies_agree_on_the_random_fixture() -> None:
    """Cap-covering and cell-and-neighbours are two routes to the same superset, so
    they must return IDENTICAL matches — a difference would be a bug in one of them,
    never a trade-off to accept."""
    with RunStore() as store:
        _random_fixture(store, with_accuracy=True)
        kwargs = dict(space_threshold_m=150.0,
                      time_before=dt.timedelta(minutes=5),
                      time_after=dt.timedelta(minutes=20),
                      use_accuracy=True)
        _, block_rows = _run(store, strategy=SpatialStrategy.CELL_NEIGHBOURS, **kwargs)
        _, cap_rows = _run(store, strategy=SpatialStrategy.CAP_COVERING, **kwargs)
        assert [r.g1_observation_id for r in block_rows] == [
            r.g1_observation_id for r in cap_rows]
        assert _pair_keys(block_rows) == _pair_keys(cap_rows)
        assert block_rows == cap_rows          # byte-identical rows, not just pairs


def test_strategy_benchmark_is_reported(capsys) -> None:
    """Reported, NOT asserted (machine-dependent): candidates examined and wall time
    for both strategies, so the default can be chosen on evidence. Run with -s to see
    the numbers."""
    with RunStore() as store:
        _random_fixture(store, count=300, seed=7)
        kwargs = dict(space_threshold_m=150.0,
                      time_before=dt.timedelta(minutes=15),
                      time_after=dt.timedelta(minutes=15))
        report: dict[str, tuple[int, int, float]] = {}
        for strategy in SpatialStrategy:
            started = time.perf_counter()
            summary, rows = _run(store, strategy=strategy, **kwargs)
            elapsed = time.perf_counter() - started
            report[strategy.value] = (summary.candidates_examined, len(rows), elapsed)
        with capsys.disabled():
            print("\n  concomitance strategy benchmark "
                  f"({summary.candidate_count_1} anchors):")
            for name, (examined, matches, elapsed) in report.items():
                print(f"    {name:16s} candidates_examined={examined:7d} "
                      f"matches={matches:5d} wall={elapsed * 1000:8.1f} ms")
        # The only observation: they agree on the answer (cost is reported, not judged).
        assert {v[1] for v in report.values()} == {report["cell_neighbours"][1]}


# --- 7 · two stores ------------------------------------------------------------------

def test_two_stores_report_the_same_identity_that_one_store_excludes() -> None:
    """The deliberate inversion: the same IMEI in two cases at the same place and time
    is precisely the finding; within ONE case it is convergence and stays excluded."""
    with RunStore() as store_1, RunStore() as store_2:
        fixture_1 = _CaseFixture(store_1, case_number="CASE-1")
        a = fixture_1.add_point(*KNOWN, _minutes(0), _minutes(5),
                                identifier="351324466102918", identifier_type="imei")
        integrate_spatial(store_1)

        fixture_2 = _CaseFixture(store_2, case_number="CASE-2")
        b = fixture_2.add_point(*_north_of(5.0), _minutes(1), _minutes(4),
                                identifier="351324466102918", identifier_type="imei")
        integrate_spatial(store_2)

        # Two stores: reported.
        rows = _assert_parity(store_1, store_2, space_threshold_m=50.0,
                              time_before=dt.timedelta(minutes=10),
                              time_after=dt.timedelta(minutes=10))
        assert _pair_keys(rows) == {frozenset((a, b))}
        assert rows[0].g1_observation_id == a and rows[0].g2_observation_id == b

        # The SAME pair within one case: excluded.
        with RunStore() as single:
            fixture = _CaseFixture(single)
            fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                              identifier="351324466102918", identifier_type="imei")
            fixture.add_point(*_north_of(5.0), _minutes(1), _minutes(4),
                              identifier="351324466102918", identifier_type="imei")
            integrate_spatial(single)
            _, excluded = _run(single, space_threshold_m=50.0)
            assert excluded == []


def test_two_stores_never_apply_the_id_order_filter() -> None:
    """Cross-case pairs are ordered by construction (anchors from case 1, partners from
    case 2), so no dedup applies — and none may drop a pair by id order."""
    with RunStore() as store_1, RunStore() as store_2:
        f1 = _CaseFixture(store_1, case_number="CASE-A")
        f2 = _CaseFixture(store_2, case_number="CASE-B")
        expected = set()
        for i in range(6):
            a = f1.add_point(*_north_of(i * 2.0), _minutes(0), _minutes(5),
                             identifier=f"+4179000{i:04d}")
            b = f2.add_point(*_north_of(i * 2.0 + 1.0), _minutes(1), _minutes(4),
                             identifier=f"+4179111{i:04d}")
            expected.add((a, b))
        integrate_spatial(store_1)
        integrate_spatial(store_2)
        summary, rows = _run(store_1, store_2, space_threshold_m=1000.0)
        assert summary.candidate_count_1 == 6 and summary.candidate_count_2 == 6
        assert len(rows) == 36              # every cross pair, none dropped
        assert expected <= {(r.g1_observation_id, r.g2_observation_id) for r in rows}


# --- 8 · presence, identity, validation ------------------------------------------------

def test_non_presence_links_are_excluded_from_concomitance() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        a = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                              identifier="+41790000001")
        b = fixture.add_point(*_north_of(10.0), _minutes(0), _minutes(5),
                              identifier="+41790000002")
        # A chat pin REFERRING to the same place at the same time: never presence.
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000003", position_link="references")
        # An alibi (E-T negation) and an S-T negation: negation never reads as presence.
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000004",
                          time_link="never_during_interval",
                          st_link="continuous_during_interval")
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000005",
                          st_link="never_during_interval")
        integrate_spatial(store)

        summary, rows = _run(store, space_threshold_m=50.0)
        assert summary.candidate_count_1 == 2 and summary.candidate_count_2 == 2
        assert _pair_keys(rows) == {frozenset((a, b))}


def test_unknown_group_token_raises_instead_of_matching_nothing() -> None:
    """A typo must raise, never return an empty result reading as 'nothing found'."""
    with RunStore() as store:
        _CaseFixture(store)
        with pytest.raises(ValueError, match="unknown tokens"):
            _run(store, group_1=EntityGroup(criteria=(
                Criterion(CriterionKind.ENTITY_TYPE, "vehicel"),)))
        with pytest.raises(ValueError, match="unknown tokens"):
            _run(store, group_2=EntityGroup(criteria=(
                Criterion(CriterionKind.IDENTIFIER_TYPE, "msisdnn"),)))
        # An identifier is free text by nature and must NOT be validated.
        _run(store, group_1=EntityGroup(criteria=(
            Criterion(CriterionKind.IDENTIFIER, "anything at all"),)))


def test_negative_thresholds_raise() -> None:
    with RunStore() as store:
        _CaseFixture(store)
        with pytest.raises(ValueError, match="space_threshold_m"):
            _run(store, space_threshold_m=-1.0)
        with pytest.raises(ValueError, match="time_before"):
            _run(store, time_before=dt.timedelta(minutes=-1))
        with pytest.raises(ValueError, match="time_after"):
            _run(store, time_after=dt.timedelta(minutes=-1))


# --- 9 · streaming, progress, cancellation, determinism ---------------------------------

def test_progress_is_called_once_per_anchor_with_a_constant_total() -> None:
    with RunStore() as store:
        _random_fixture(store, count=20, seed=3)
        calls: list[tuple[int, int]] = []
        summary, _ = _run(store, space_threshold_m=500.0,
                          on_progress=lambda done, total: calls.append((done, total)))
        assert len(calls) == summary.candidate_count_1
        assert [c[0] for c in calls] == list(range(1, summary.candidate_count_1 + 1))
        assert {c[1] for c in calls} == {summary.candidate_count_1}


def test_cancellation_stops_early_and_keeps_the_partial_result() -> None:
    with RunStore() as store:
        _random_fixture(store, count=40, seed=5)
        seen = {"anchors": 0}

        def should_cancel() -> bool:
            seen["anchors"] += 1
            return seen["anchors"] > 5     # cancel after 5 anchors

        sink = _CollectingSink()
        summary = find_concomitant(
            store, group_1=EntityGroup(), group_2=EntityGroup(),
            space_threshold_m=800.0,
            time_before=dt.timedelta(minutes=30),
            time_after=dt.timedelta(minutes=30),
            sink=sink, should_cancel=should_cancel)

        assert summary.cancelled is True
        assert summary.match_count == len(sink.rows)     # the partial count is real
        # Strictly fewer than a full run would have produced — work stopped early.
        full, full_rows = _run(store, space_threshold_m=800.0,
                               time_before=dt.timedelta(minutes=30),
                               time_after=dt.timedelta(minutes=30))
        assert full.cancelled is False
        assert 0 < summary.match_count < full.match_count
        # And what it did emit is exactly the prefix of the complete run.
        assert sink.rows == full_rows[:len(sink.rows)]


def test_output_is_deterministic_across_runs() -> None:
    with RunStore() as store:
        _random_fixture(store, with_accuracy=True)
        kwargs = dict(space_threshold_m=200.0,
                      time_before=dt.timedelta(minutes=5),
                      time_after=dt.timedelta(minutes=25),
                      use_accuracy=True)
        _, first = _run(store, **kwargs)
        _, second = _run(store, **kwargs)
        assert first == second


def test_symmetric_window_output_keeps_the_old_observation_pair_order() -> None:
    """With a SYMMETRIC window the emitted stream is sorted by
    ``(observation_a, observation_b)`` with the smaller id first — the old engine's
    documented order, now produced WITHOUT buffering the results.

    WHY only symmetric: when before != after the two orientations of a pair are no
    longer interchangeable, so a pair that satisfies the window only the other way
    round is emitted partner-first (its ``time_delta_s`` would otherwise contradict
    the window it matched). The stream is still fully deterministic — it is ordered by
    (anchor, partner) — but the smaller id is no longer always first.
    """
    with RunStore() as store:
        _random_fixture(store)
        _, rows = _run(store, space_threshold_m=200.0,
                       time_before=dt.timedelta(minutes=15),
                       time_after=dt.timedelta(minutes=15))
        assert rows
        keys = [(r.g1_observation_id, r.g2_observation_id) for r in rows]
        assert all(a < b for a, b in keys)
        assert keys == sorted(keys)


def test_max_cells_tunes_the_covering_but_never_the_membership() -> None:
    with RunStore() as store:
        _random_fixture(store)
        kwargs = dict(space_threshold_m=150.0,
                      time_before=dt.timedelta(minutes=15),
                      time_after=dt.timedelta(minutes=15),
                      strategy=SpatialStrategy.CAP_COVERING)
        _, default = _run(store, **kwargs)
        _, coarse = _run(store, max_cells=1, **kwargs)
        assert default == coarse
