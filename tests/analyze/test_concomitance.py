"""Tests for model_atlas/analyze/concomitance.py (T41 — the Verify list).

Covers:     pairs inside both thresholds found / outside either threshold not,
            the Q3/Q4 presence split (``references`` pins and
            ``never_during_interval`` assertions never reported), the same-identity
            exclusion, the entity selector axes (+ the unknown-token guard),
            deterministic pair ordering, the CSV export, and PARITY between the
            S2 cap-covering range-scan path and a brute-force O(n²) reference on a
            seeded random fixture (including max_cells insensitivity).
Uses:       ``model_atlas.analyze.concomitance``, ``model_atlas.core`` (ids,
            vocabularies, store) and ``model_atlas.integrate.spatial`` — the
            fixture writes L1 rows and builds the S2 index through the REAL
            ``integrate_spatial`` driver, so query-vs-build bit-parity is
            exercised end to end.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import time

import pytest

from model_atlas.analyze.concomitance import (
    EARTH_RADIUS_M,
    EntitySelector,
    ResultSet,
    find_concomitant,
    great_circle_distance_m,
)
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


def _minutes(count: float) -> int:
    """Unix µs ``count`` minutes after the BASE_US origin (exact for the ints used)."""
    return BASE_US + int(count * 60_000_000)


def _north_of(metres: float) -> tuple[float, float]:
    """The point ``metres`` due north of KNOWN (an exact great-circle offset)."""
    return (KNOWN[0] + metres / M_PER_DEG_LAT, KNOWN[1])


class _CaseFixture:
    """L0 scaffolding + an ``add_point`` writer for one candidate assertion.

    Writes the full FK chain (case -> run -> source/preset -> raw_record ->
    entity/expressions -> assertion) through the layer-typed store writers; the
    caller runs ``integrate_spatial`` once afterwards so the spatial_integration
    and spatial_index rows come from the REAL build path.
    """

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.line_no = 0
        case_id = ids.case_metadata_id()
        store.write_case_metadata(V.CaseMetadata(case_id=case_id,
                                                 case_number="T41-CASE"))
        run_id_ = ids.run_id()
        store.write_run(V.Run(run_id=run_id_, case_id=case_id))
        self.source_id = ids.source_id("/case/t41_fixture.csv", None, None)
        store.write_source(V.Source(source_id=self.source_id, run_id=run_id_,
                                    input_file_path="/case/t41_fixture.csv"))
        self.preset_key = "t41_fixture"
        self.preset_id = ids.preset_id(self.preset_key, "1")
        store.write_preset(V.Preset(preset_id=self.preset_id, key=self.preset_key,
                                    name=None, version="1",
                                    definition_digest="sha256-fixture"))

    def add_point(self, latitude: float, longitude: float,
                  lower_us: int, upper_us: int, *,
                  identifier: str | None,
                  identifier_type: str = "msisdn",
                  entity_type: str = "device",
                  position_link: str = "at",
                  time_link: str = "observed_at",
                  st_link: str = "instant") -> str:
        """One placed-and-timed assertion; returns its assertion_id."""
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
        for ordinal, (part, raw) in enumerate((
                ("latitude_wgs84", str(latitude)),
                ("longitude_wgs84", str(longitude)))):
            self.store.write_spatial_component(V.SpatialComponent(
                spatial_component_id=ids.spatial_component_id(sx_id, ordinal),
                spatial_expression_id=sx_id, ordinal=ordinal,
                part=part, raw_value=raw))
        tx_id = ids.temporal_expression_id(natural_key)
        self.store.write_temporal_expression(V.TemporalExpression(
            temporal_expression_id=tx_id, kind="interval"))
        self.store.write_timespan_integration(V.TimespanIntegration(
            timespan_integration_id=ids.timespan_integration_id(
                tx_id, "fixture/1.0"),
            temporal_expression_id=tx_id, method="fixture/1.0",
            lower_unix_utc_us=lower_us, upper_unix_utc_us=upper_us))
        assertion_id = ids.assertion_id(record_id, self.preset_key, 0)
        self.store.write_assertion(V.Assertion(
            assertion_id=assertion_id, raw_record_id=record_id,
            preset_id=self.preset_id, output_ordinal=0, entity_id=entity_id,
            content_hash=f"ch-{self.line_no}", claim_digest=f"cd-{self.line_no}",
            spatial_expression_id=sx_id, temporal_expression_id=tx_id,
            entity_role="subject", entity_position_link=position_link,
            entity_time_link=time_link, spatial_temporal_link=st_link))
        return assertion_id


def _pair_keys(result: ResultSet) -> set[tuple[str, str]]:
    return {(pair.assertion_a, pair.assertion_b) for pair in result.pairs}


# --- thresholds: inside found, outside (either axis) not -----------------------

def test_pair_within_both_thresholds_is_found_with_exact_values() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        a = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                              identifier="+41790000001")
        b = fixture.add_point(*_north_of(10.0), _minutes(2), _minutes(6),
                              identifier="+41790000002")
        integrate_spatial(store)

        result = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))
        assert result.candidate_count == 2
        assert _pair_keys(result) == {(min(a, b), max(a, b))}
        (pair,) = result.pairs
        assert pair.distance_m == pytest.approx(10.0, abs=0.01)
        assert pair.time_gap_us == 0                 # overlapping intervals
        assert pair.assertion_a < pair.assertion_b   # the deterministic pair order
        assert {pair.entity_a.identifier, pair.entity_b.identifier} == {
            "+41790000001", "+41790000002"}


def test_point_just_outside_space_threshold_is_not_found() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001")
        fixture.add_point(*_north_of(60.0), _minutes(0), _minutes(5),
                          identifier="+41790000002")
        integrate_spatial(store)

        narrow = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))
        assert narrow.pairs == ()
        # The SAME points fall inside a 70 m threshold — the cut is the threshold,
        # not an accident of the covering.
        wide = find_concomitant(store, space_threshold_m=70.0,
                                time_threshold=dt.timedelta(minutes=10))
        assert len(wide.pairs) == 1


def test_point_just_outside_time_threshold_is_not_found() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        a = fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                              identifier="+41790000001")
        c = fixture.add_point(*_north_of(10.0), _minutes(20), _minutes(25),
                              identifier="+41790000002")
        integrate_spatial(store)

        # Gap = 15 min (05 -> 20): outside a 10 min threshold...
        narrow = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))
        assert narrow.pairs == ()
        # ...inside a 20 min one, with the EXACT gap reported in µs.
        wide = find_concomitant(store, space_threshold_m=50.0,
                                time_threshold=dt.timedelta(minutes=20))
        assert _pair_keys(wide) == {(min(a, c), max(a, c))}
        assert wide.pairs[0].time_gap_us == 15 * 60_000_000


# --- the Q3/Q4 presence split ---------------------------------------------------

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
        # An alibi (E-T negation) and an S-T negation at the same coords/time:
        # negation never reads as presence (D37).
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000004",
                          time_link="never_during_interval",
                          st_link="continuous_during_interval")
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000005",
                          st_link="never_during_interval")
        integrate_spatial(store)

        result = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))
        # Only the two presence assertions survive as candidates and pair up; the
        # three non-presence rows appear in NO pair despite matching coordinates.
        assert result.candidate_count == 2
        assert _pair_keys(result) == {(min(a, b), max(a, b))}


def test_same_normalised_identity_is_never_paired_with_itself() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        # One phone disclosed twice (two sources, two assertions): convergence,
        # not concomitance.
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001")
        fixture.add_point(*_north_of(5.0), _minutes(1), _minutes(4),
                          identifier="+41790000001")
        integrate_spatial(store)

        result = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))
        assert result.candidate_count == 2
        assert result.pairs == ()


# --- the entity selector ----------------------------------------------------------

def test_entity_selector_narrows_the_candidates() -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001")
        fixture.add_point(*_north_of(5.0), _minutes(0), _minutes(5),
                          identifier="+41790000002")
        vehicle = fixture.add_point(*_north_of(10.0), _minutes(0), _minutes(5),
                                    identifier="GE123456",
                                    identifier_type="plate",
                                    entity_type="vehicle")
        camera = fixture.add_point(*_north_of(15.0), _minutes(0), _minutes(5),
                                   identifier="Lobby-2",
                                   identifier_type="camera",
                                   entity_type="camera")
        integrate_spatial(store)

        kwargs = dict(space_threshold_m=50.0,
                      time_threshold=dt.timedelta(minutes=10))
        # No selector: every candidate takes part (narrowing is opt-in).
        assert find_concomitant(store, **kwargs).candidate_count == 4
        # By type (enum members accepted): only vehicle+camera remain.
        by_type = find_concomitant(store, entity_selector=EntitySelector(
            types=(V.EntityType.VEHICLE, "camera")), **kwargs)
        assert by_type.candidate_count == 2
        assert _pair_keys(by_type) == {(min(vehicle, camera), max(vehicle, camera))}
        # By identifier_type.
        by_id_type = find_concomitant(store, entity_selector=EntitySelector(
            identifier_types=("plate",)), **kwargs)
        assert by_id_type.candidate_count == 1
        assert by_id_type.pairs == ()
        # By explicit identifier list (normalised form).
        by_ids = find_concomitant(store, entity_selector=EntitySelector(
            identifiers=("+41790000001", "+41790000002")), **kwargs)
        assert by_ids.candidate_count == 2
        assert len(by_ids.pairs) == 1


def test_unknown_selector_token_raises_instead_of_matching_nothing() -> None:
    with RunStore() as store:
        _CaseFixture(store)
        with pytest.raises(ValueError):
            find_concomitant(store,
                             entity_selector=EntitySelector(types=("vehicel",)),
                             space_threshold_m=10.0,
                             time_threshold=dt.timedelta(minutes=1))


def test_negative_thresholds_raise() -> None:
    with RunStore() as store:
        _CaseFixture(store)
        with pytest.raises(ValueError):
            find_concomitant(store, space_threshold_m=-1.0,
                             time_threshold=dt.timedelta(minutes=1))
        with pytest.raises(ValueError):
            find_concomitant(store, space_threshold_m=1.0,
                             time_threshold=dt.timedelta(minutes=-1))


# --- parity with a brute-force O(n²) reference ------------------------------------

# The reference's OWN candidate query — deliberately written out longhand (not
# imported from the engine) so a predicate bug there cannot hide in the parity.
_BRUTE_CANDIDATE_SQL = """
SELECT a.assertion_id, e.identifier, e.identifier_type,
       si.latitude_wgs84, si.longitude_wgs84,
       ti.lower_unix_utc_us, ti.upper_unix_utc_us
FROM assertion a
JOIN entity e ON e.entity_id = a.entity_id
JOIN spatial_integration si ON si.spatial_expression_id = a.spatial_expression_id
JOIN spatial_index six ON six.spatial_integration_id = si.spatial_integration_id
JOIN timespan_integration ti ON ti.temporal_expression_id = a.temporal_expression_id
WHERE ti.lower_unix_utc_us IS NOT NULL AND ti.upper_unix_utc_us IS NOT NULL
  AND (a.entity_position_link IS NULL OR a.entity_position_link != 'references')
  AND (a.entity_time_link IS NULL
       OR a.entity_time_link != 'never_during_interval')
  AND (a.spatial_temporal_link IS NULL
       OR a.spatial_temporal_link != 'never_during_interval')
"""


def _brute_force_pairs(store: RunStore, space_threshold_m: float,
                       gap_limit_us: int) -> set[tuple[str, str, float, int]]:
    """The O(n²) reference: every candidate against every other, no index at all.

    Shares only ``great_circle_distance_m`` with the engine — the parity target is
    the CANDIDATE-PAIRING path (covering + range scans + dedup), not the distance
    formula, so sharing the formula keeps the comparison exact.
    """
    rows = store.connection.execute(_BRUTE_CANDIDATE_SQL).fetchall()
    pairs: set[tuple[str, str, float, int]] = set()
    for i in range(len(rows)):
        for j in range(len(rows)):
            if i == j:
                continue
            (id_a, ident_a, ident_type_a, lat_a, lon_a, lo_a, up_a) = rows[i]
            (id_b, ident_b, ident_type_b, lat_b, lon_b, lo_b, up_b) = rows[j]
            if id_a >= id_b:      # each unordered pair once, smaller id first
                continue
            if (ident_a is not None and ident_a == ident_b
                    and ident_type_a == ident_type_b):
                continue          # the same-identity exclusion
            distance = great_circle_distance_m(lat_a, lon_a, lat_b, lon_b)
            if distance > space_threshold_m:
                continue
            gap = max(lo_a - up_b, lo_b - up_a, 0)
            if gap > gap_limit_us:
                continue
            pairs.add((id_a, id_b, round(distance, 6), gap))
    return pairs


def _as_key_set(result: ResultSet) -> set[tuple[str, str, float, int]]:
    return {(p.assertion_a, p.assertion_b, round(p.distance_m, 6), p.time_gap_us)
            for p in result.pairs}


def test_s2_path_matches_brute_force_on_a_random_fixture() -> None:
    rng = random.Random(41)   # seeded: the fixture (and the parity) is reproducible
    with RunStore() as store:
        fixture = _CaseFixture(store)
        # 60 mixed points in a ~2 km box over a 2 h window; 7 recurring identities
        # so the same-identity exclusion is exercised INSIDE the parity; every 8th
        # row is non-presence so the exclusion filter is exercised on both paths.
        links = ("at", "at", "at", "at", "at", "at", "at", "references")
        for i in range(60):
            latitude = KNOWN[0] + rng.uniform(-0.010, 0.010)
            longitude = KNOWN[1] + rng.uniform(-0.015, 0.015)
            lower = _minutes(rng.uniform(0.0, 120.0))
            upper = lower + int(rng.uniform(0.0, 10.0) * 60_000_000)
            fixture.add_point(latitude, longitude, lower, upper,
                              identifier=f"+4179000000{i % 7}",
                              position_link=links[i % len(links)])
        integrate_spatial(store)

        space_threshold_m = 150.0
        time_threshold = dt.timedelta(minutes=15)
        gap_limit_us = time_threshold // dt.timedelta(microseconds=1)

        started = time.perf_counter()
        result = find_concomitant(store, space_threshold_m=space_threshold_m,
                                  time_threshold=time_threshold)
        s2_seconds = time.perf_counter() - started
        started = time.perf_counter()
        expected = _brute_force_pairs(store, space_threshold_m, gap_limit_us)
        brute_seconds = time.perf_counter() - started

        # PARITY: identical pair sets, distances and gaps included.
        assert expected                      # the fixture must actually produce pairs
        assert _as_key_set(result) == expected
        # Determinism: sorted output, smaller assertion id always first, and a
        # second run yields the byte-identical tuple.
        assert list(result.pairs) == sorted(
            result.pairs, key=lambda p: (p.assertion_a, p.assertion_b))
        assert all(p.assertion_a < p.assertion_b for p in result.pairs)
        rerun = find_concomitant(store, space_threshold_m=space_threshold_m,
                                 time_threshold=time_threshold)
        assert rerun.pairs == result.pairs
        # The coverer budget tunes the covering only — never the membership.
        coarse = find_concomitant(store, space_threshold_m=space_threshold_m,
                                  time_threshold=time_threshold, max_cells=1)
        assert coarse.pairs == result.pairs

        # Timing note (not asserted — machine-dependent): on a fixture THIS small
        # the brute force is actually faster (~1 ms vs ~50 ms at n=60): the S2
        # path pays a constant per-anchor cost (the pure-Python RegionCoverer,
        # ~1 ms) that dwarfs 1 770 haversines. The point is the SCALING: the S2
        # path is LINEAR in the candidates (per-anchor covering + O(max_cells)
        # B-tree range scans of the whole index), the reference QUADRATIC — on a
        # real run DB (10⁵-10⁶ indexed points) only the former stays usable.
        # Both figures are computed above (s2_seconds / brute_seconds) so a
        # profiling session can log them.
        assert s2_seconds >= 0.0 and brute_seconds >= 0.0


# --- the CSV export ------------------------------------------------------------------

def test_csv_export_writes_header_and_pair_rows(tmp_path) -> None:
    with RunStore() as store:
        fixture = _CaseFixture(store)
        fixture.add_point(*KNOWN, _minutes(0), _minutes(5),
                          identifier="+41790000001")
        fixture.add_point(*_north_of(10.0), _minutes(2), _minutes(6),
                          identifier="+41790000002")
        integrate_spatial(store)
        result = find_concomitant(store, space_threshold_m=50.0,
                                  time_threshold=dt.timedelta(minutes=10))

        target = result.to_csv(tmp_path / "concomitant.csv")
        raw = target.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")   # utf-8-sig BOM (house rule)
        lines = raw.decode("utf-8-sig").splitlines()
        assert lines[0].startswith("entity_a_id,entity_a_identifier")
        assert len(lines) == 1 + len(result.pairs) == 2
        assert "+41790000001" in lines[1] and "+41790000002" in lines[1]
        assert result.pairs[0].assertion_a in lines[1]
