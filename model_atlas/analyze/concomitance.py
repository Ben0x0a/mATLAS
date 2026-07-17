"""The spatiotemporal concomitance engine (T41): who was together, where and when.

Defines:    the public query ``find_concomitant(store, *, entity_selector,
            space_threshold_m, time_threshold, ...) -> ResultSet`` plus its input/
            output shapes (``EntitySelector``, ``EntityRef``, ``ConcomitantPair``,
            ``ResultSet`` with its CSV export) and the exact great-circle helper
            ``great_circle_distance_m``. ALL S2 level-walking/covering lives here —
            the schema stores only the leaf cell (decision 31).
Used by:    the future concomitance GUI panel (T72 — MVC: the panel only calls this)
            and tests/analyze/test_concomitance.py.
Depends on: ``model_atlas.core.store`` (the read connection), ``model_atlas.core.
            vocabularies`` (the presence-exclusion link tokens), ``model_atlas.
            integrate.spatial`` (``s2_cell_to_stored`` — the ONE signed-int64 recipe,
            imported so build and query agree bit-for-bit) and ``s2sphere``.

The algorithm (no O(n²) main path)
----------------------------------
1. ONE indexed SQL pass selects the candidate assertions: entity selector applied,
   presence-only (see below), both coordinates and both time bounds present, joined
   through ``spatial_integration``/``spatial_index``/``timespan_integration``.
2. For each candidate point an ``s2sphere.Cap`` of radius ``space_threshold_m`` is
   covered by a ``RegionCoverer`` into at most ``max_cells`` cells; each covering
   cell becomes one leaf range ``[range_min, range_max]`` in SIGNED storage form.
3. Each range drives an indexed ``WHERE s2_cell_id BETWEEN ? AND ?`` scan (the D31
   contract: hierarchy is implicit in the id, so the coarse-cell walk is a cheap
   B-tree range scan, never a table scan), pre-filtered in SQL by the temporal
   window and by ``assertion_id > anchor`` so each unordered pair is visited once.
4. The survivors are filtered EXACTLY: great-circle distance ≤ threshold and
   interval gap ≤ threshold (overlapping intervals gap 0). The covering is a
   superset of the cap, so step 3 can only over-select — never miss.

Presence exclusion (the worked examples' Q3/Q4 split)
-----------------------------------------------------
"Who was together" is a PRESENCE question, so three link tokens exclude a candidate:
``entity_position_link = 'references'`` (a pin/search points at a place — the entity
was never there) and ``never_during_interval`` on EITHER the entity-time or the
spatial-temporal edge (both read as negation — D37: negation never reads as
presence). A NULL link is NOT excluded: unknown is not non-presence, and silently
dropping unmapped rows would hide evidence (process everything by default).

Pair identity and ordering (deterministic by construction)
----------------------------------------------------------
A result pair is (assertion_a, assertion_b) with ``assertion_a < assertion_b`` by
plain string comparison of the uuid text — the anchor loop only accepts partners
with a GREATER id, so no pair appears twice, no assertion pairs with itself, and
the ``ResultSet`` rows sort by that same (assertion_a, assertion_b) key: the same
run DB always yields the byte-identical result list. Two assertions carrying the
SAME normalised (identifier_type, identifier) are never paired — an identity
"together with itself" (a phone seen by two sources) is convergence, not
concomitance; NULL identifiers stay pairable (two unknown devices may be two
devices).
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import math
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

import s2sphere

from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore
from model_atlas.integrate.spatial import s2_cell_to_stored

_LOG = logging.getLogger(__name__)

# Mean Earth radius (IUGG 1980 mean radius R1, metres). WHY this constant: S2 works
# on a unit sphere, so the cap radius (step 2) AND the exact great-circle filter
# (step 4) must use the SAME sphere — one constant guarantees the covering can never
# under-cover relative to the filter. WGS84 great-circle arcs differ from this
# sphere by < 0.6 %, well inside any concomitance threshold's intent; the constant
# is documented so the tolerance is auditable.
EARTH_RADIUS_M = 6_371_008.8

# The coverer's cell budget: more cells = a tighter covering (fewer false
# candidates to reject exactly) but more BETWEEN scans per anchor. 8 is the
# s2sphere default and a good balance for point-radius caps.
DEFAULT_MAX_CELLS = 8

# --- the presence-exclusion tokens (from the vocabularies, never re-typed) ---
_REFERENCES_TOKEN = V.EntityPositionLink.REFERENCES.value
_NEVER_ET_TOKEN = V.EntityTimeLink.NEVER_DURING_INTERVAL.value
_NEVER_ST_TOKEN = V.SpatialTemporalLink.NEVER_DURING_INTERVAL.value


# --- the public input/output shapes ------------------------------------------

@dataclass(frozen=True)
class EntitySelector:
    """Which entities take part in the query. Empty axes = no constraint.

    Each axis is a sequence of tokens (plain strings or the vocabulary enums);
    non-empty axes are AND-ed: the candidate entity must match EVERY stated axis.
    ``identifiers`` matches the NORMALISED ``entity.identifier`` column (E.164,
    lowercase email…), so a NULL-identifier mention never matches an explicit list.
    """

    types: Sequence[str | V.EntityType] = ()
    identifier_types: Sequence[str | V.IdentifierType] = ()
    identifiers: Sequence[str] = ()


@dataclass(frozen=True)
class EntityRef:
    """One entity as a result row names it (the lean D17 identity columns)."""

    entity_id: str
    identifier: str | None
    identifier_type: str
    type: str


@dataclass(frozen=True)
class ConcomitantPair:
    """One concomitance finding: two assertions near in space AND time.

    ``distance_m`` is the exact great-circle distance between the two integrated
    points (mean-radius sphere, ``EARTH_RADIUS_M``); ``time_gap_us`` is the gap
    between the two time intervals in canonical µs — 0 when they overlap.
    ``assertion_a < assertion_b`` always (the deterministic pair order).
    """

    entity_a: EntityRef
    entity_b: EntityRef
    distance_m: float
    time_gap_us: int
    assertion_a: str
    assertion_b: str


# The CSV header, one column per exported field, in the row-shape order the task
# fixes: entity A, entity B, distance, gap, the two assertion ids.
_CSV_HEADER: tuple[str, ...] = (
    "entity_a_id", "entity_a_identifier", "entity_a_identifier_type", "entity_a_type",
    "entity_b_id", "entity_b_identifier", "entity_b_identifier_type", "entity_b_type",
    "distance_m", "time_gap_us", "assertion_a", "assertion_b",
)


@dataclass(frozen=True)
class ResultSet:
    """The full result of one ``find_concomitant`` call, with its parameters.

    ``pairs`` is sorted by (assertion_a, assertion_b) — deterministic and stable
    across runs on the same DB. The thresholds and the candidate count are carried
    so an export (or the GUI) can always state WHAT question the rows answer.
    """

    pairs: tuple[ConcomitantPair, ...]
    space_threshold_m: float
    time_threshold: dt.timedelta
    candidate_count: int

    def to_csv(self, path: str | Path) -> Path:
        """Write the pairs as a CSV file and return its path.

        utf-8-sig (house rule): the BOM lets Excel/Notepad auto-detect the encoding;
        no consumer of this export is known to reject a BOM.
        """
        target = Path(path)
        with open(target, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(_CSV_HEADER)
            for pair in self.pairs:
                writer.writerow((
                    pair.entity_a.entity_id, pair.entity_a.identifier,
                    pair.entity_a.identifier_type, pair.entity_a.type,
                    pair.entity_b.entity_id, pair.entity_b.identifier,
                    pair.entity_b.identifier_type, pair.entity_b.type,
                    pair.distance_m, pair.time_gap_us,
                    pair.assertion_a, pair.assertion_b,
                ))
        return target


# --- exact geometry ------------------------------------------------------------

def great_circle_distance_m(lat_a: float, lon_a: float,
                            lat_b: float, lon_b: float) -> float:
    """Great-circle distance between two WGS84 points, in metres.

    HOW: the haversine formula on the mean-radius sphere (``EARTH_RADIUS_M``) —
    numerically stable for the SHORT arcs concomitance cares about (the naive
    spherical-law-of-cosines loses precision below ~1 m separations).
    WHY this sphere and not the WGS84 ellipsoid: the S2 cap covering lives on the
    same sphere, so filter and covering agree exactly (see the module docstring).
    """
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    h = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


# --- the candidate row (private working shape) ----------------------------------

@dataclass(frozen=True)
class _Candidate:
    """One candidate assertion: identity + integrated point + canonical interval."""

    assertion_id: str
    entity: EntityRef
    latitude: float
    longitude: float
    lower_us: int
    upper_us: int


# --- SQL assembly ----------------------------------------------------------------
# One shared FROM/WHERE for BOTH the candidate load and the per-anchor range scans,
# so anchor and partner are drawn from the IDENTICAL population — an asymmetry here
# (e.g. the partner path requiring an index row the candidate path did not) would
# silently lose pairs. The spatial_index join is part of the candidate definition:
# D31 writes one index row per integrated point, so a point without one is not a
# queryable point.

_FROM_JOINS = (
    " FROM assertion a"
    " JOIN entity e ON e.entity_id = a.entity_id"
    " JOIN spatial_integration si"
    "   ON si.spatial_expression_id = a.spatial_expression_id"
    " JOIN spatial_index six"
    "   ON six.spatial_integration_id = si.spatial_integration_id"
    " JOIN timespan_integration ti"
    "   ON ti.temporal_expression_id = a.temporal_expression_id"
)

# The presence + completeness predicate. SQLite's ``IS NOT`` is the NULL-safe
# inequality, so a NULL link SURVIVES each exclusion (unknown is not non-presence —
# a plain ``!=`` would silently drop every unmapped row). The coordinate null checks
# guard the exact-distance step against a hand-written index row missing its point.
_PRESENCE_WHERE = (
    " WHERE ti.lower_unix_utc_us IS NOT NULL AND ti.upper_unix_utc_us IS NOT NULL"
    " AND si.latitude_wgs84 IS NOT NULL AND si.longitude_wgs84 IS NOT NULL"
    " AND a.entity_position_link IS NOT ?"
    " AND a.entity_time_link IS NOT ?"
    " AND a.spatial_temporal_link IS NOT ?"
)
_PRESENCE_PARAMS: tuple[str, ...] = (
    _REFERENCES_TOKEN, _NEVER_ET_TOKEN, _NEVER_ST_TOKEN)


def _tokens(values: Iterable[object], enum_type: type[Enum] | None,
            axis: str) -> tuple[str, ...]:
    """The bare stored tokens of one selector axis, validated against its vocabulary.

    WHY validate: a typo'd type ('vehicel') would otherwise match nothing and
    return an EMPTY result that reads as 'no concomitance found' — the classic
    silent loss; raising at the boundary keeps the answer honest.
    """
    rendered = tuple(str(getattr(value, "value", value)) for value in values)
    if enum_type is not None:
        allowed = {member.value for member in enum_type}
        unknown = sorted(set(rendered) - allowed)
        if unknown:
            raise ValueError(
                f"entity selector {axis} contains unknown tokens {unknown}; "
                f"allowed: {sorted(allowed)}")
    return rendered


def _selector_clause(selector: EntitySelector | None) -> tuple[str, list[str]]:
    """The AND-ed selector predicate (possibly empty) and its bind parameters."""
    if selector is None:
        return "", []
    clauses: list[str] = []
    params: list[str] = []
    for column, values in (
            ("e.type", _tokens(selector.types, V.EntityType, "types")),
            ("e.identifier_type",
             _tokens(selector.identifier_types, V.IdentifierType,
                     "identifier_types")),
            ("e.identifier", _tokens(selector.identifiers, None, "identifiers"))):
        if values:
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f" AND {column} IN ({placeholders})")
            params.extend(values)
    return "".join(clauses), params


def _load_candidates(connection: sqlite3.Connection,
                     selector: EntitySelector | None,
                     ) -> dict[str, _Candidate]:
    """Every candidate assertion, keyed by assertion_id, in id order.

    One indexed pass; the ORDER BY makes the anchor iteration (and hence any
    logging/debug trace) deterministic — the result order itself is re-sorted at
    the end regardless.
    """
    selector_sql, selector_params = _selector_clause(selector)
    rows = connection.execute(
        "SELECT a.assertion_id, e.entity_id, e.identifier, e.identifier_type,"
        "       e.type, si.latitude_wgs84, si.longitude_wgs84,"
        "       ti.lower_unix_utc_us, ti.upper_unix_utc_us"
        + _FROM_JOINS + _PRESENCE_WHERE + selector_sql
        + " ORDER BY a.assertion_id",
        (*_PRESENCE_PARAMS, *selector_params))
    candidates: dict[str, _Candidate] = {}
    for (assertion_id, entity_id, identifier, identifier_type, entity_type,
         latitude, longitude, lower_us, upper_us) in rows:
        candidates[assertion_id] = _Candidate(
            assertion_id=assertion_id,
            entity=EntityRef(entity_id, identifier, identifier_type, entity_type),
            latitude=latitude, longitude=longitude,
            lower_us=lower_us, upper_us=upper_us)
    return candidates


# --- the S2 cap covering (ALL level-walking lives here — D31) --------------------

def _cap_cell_ranges(latitude: float, longitude: float, radius_m: float,
                     max_cells: int) -> list[tuple[int, int]]:
    """The signed leaf-id ranges covering a cap of ``radius_m`` around a point.

    HOW: cap radius in radians = arc/R on the SAME sphere as the exact filter;
    ``RegionCoverer`` (default level bounds 0..30, ``max_cells`` capped) returns a
    small covering; each covering cell's descendants are the contiguous leaf range
    ``[range_min, range_max]``, converted to storage form by the ONE imported
    signing helper. WHY the signed form is BETWEEN-safe: a cell's range never
    crosses a face (top-bit) boundary, so both bounds keep their relative order
    (the ``integrate/spatial.py`` D31 contract).
    """
    axis = s2sphere.LatLng.from_degrees(latitude, longitude).to_point()
    angle = s2sphere.Angle.from_radians(radius_m / EARTH_RADIUS_M)
    cap = s2sphere.Cap.from_axis_angle(axis, angle)
    coverer = s2sphere.RegionCoverer()
    coverer.max_cells = max_cells
    return [
        (s2_cell_to_stored(cell.range_min().id()),
         s2_cell_to_stored(cell.range_max().id()))
        for cell in coverer.get_covering(cap)
    ]


# --- exact temporal gap -----------------------------------------------------------

def _interval_gap_us(a: _Candidate, b: _Candidate) -> int:
    """The gap between two closed intervals in µs; 0 when they overlap or touch.

    HOW: for disjoint intervals exactly one of (a.lower - b.upper, b.lower -
    a.upper) is positive and IS the gap; when they overlap both are ≤ 0 and the
    max clamps to 0. Exact integer arithmetic — no float µs.
    """
    return max(a.lower_us - b.upper_us, b.lower_us - a.upper_us, 0)


def _same_identity(a: EntityRef, b: EntityRef) -> bool:
    """True when both sides carry the SAME normalised working identity.

    An identity 'together with itself' (one phone disclosed by two sources) is
    convergence, not concomitance, so such pairs are dropped; NULL identifiers are
    never 'the same' (two unmarked devices may genuinely be two devices).
    """
    return (a.identifier is not None
            and a.identifier == b.identifier
            and a.identifier_type == b.identifier_type)


# --- the public query --------------------------------------------------------------

def find_concomitant(
    store: RunStore,
    *,
    entity_selector: EntitySelector | None = None,
    space_threshold_m: float,
    time_threshold: dt.timedelta,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> ResultSet:
    """Every pair of presence assertions within BOTH thresholds, deterministically.

    ``entity_selector`` narrows the candidate entities (None/empty = everything —
    narrowing is opt-in); ``space_threshold_m`` is the exact great-circle limit;
    ``time_threshold`` the largest allowed gap between the two time intervals
    (overlap counts as gap zero); ``max_cells`` tunes the coverer only — a larger
    budget tightens the covering, it never changes the result (the exact filter
    decides membership).

    See the module docstring for the four-step algorithm, the presence exclusion
    and the pair-ordering contract.
    """
    # Invariant guards: a negative threshold has no geometric meaning — proceeding
    # would silently return an empty result that reads as 'nothing found'.
    if space_threshold_m < 0:
        raise ValueError(f"space_threshold_m must be >= 0, got {space_threshold_m}")
    if time_threshold < dt.timedelta(0):
        raise ValueError(f"time_threshold must be >= 0, got {time_threshold}")
    if max_cells < 1:
        raise ValueError(f"max_cells must be >= 1, got {max_cells}")
    # timedelta floor-division by 1 µs is EXACT (timedelta stores integer µs);
    # total_seconds() would round through a float.
    gap_limit_us = time_threshold // dt.timedelta(microseconds=1)

    connection = store.connection
    candidates = _load_candidates(connection, entity_selector)
    selector_sql, selector_params = _selector_clause(entity_selector)

    pairs: list[ConcomitantPair] = []
    for anchor in candidates.values():
        ranges = _cap_cell_ranges(
            anchor.latitude, anchor.longitude, space_threshold_m, max_cells)
        # One scan per anchor, OR-ing the few BETWEEN ranges: SQLite runs each as
        # an ix_spatial_index_cell B-tree range scan. The SQL pre-filter is the
        # COARSE cut (covering cells + temporal window + the once-per-pair id
        # order); the exact great-circle/gap cut below decides membership.
        range_sql = " OR ".join(
            "six.s2_cell_id BETWEEN ? AND ?" for _ in ranges)
        range_params = [bound for cell_range in ranges for bound in cell_range]
        rows = connection.execute(
            "SELECT a.assertion_id" + _FROM_JOINS + _PRESENCE_WHERE + selector_sql
            + f" AND ({range_sql})"
            + " AND a.assertion_id > ?"
            + " AND ti.lower_unix_utc_us <= ? AND ti.upper_unix_utc_us >= ?",
            (*_PRESENCE_PARAMS, *selector_params, *range_params,
             anchor.assertion_id,
             anchor.upper_us + gap_limit_us, anchor.lower_us - gap_limit_us))
        for (partner_id,) in rows:
            partner = candidates.get(partner_id)
            # Invariant: the scan's FROM/WHERE equals the candidate load's, so
            # every partner id MUST be a loaded candidate — a miss means the two
            # predicates drifted apart (a lost-pair bug), never a normal outcome.
            assert partner is not None, (
                f"partner {partner_id} matched the range scan but is not a "
                f"candidate: the scan and load predicates drifted")
            if _same_identity(anchor.entity, partner.entity):
                continue
            distance_m = great_circle_distance_m(
                anchor.latitude, anchor.longitude,
                partner.latitude, partner.longitude)
            if distance_m > space_threshold_m:
                continue
            gap_us = _interval_gap_us(anchor, partner)
            if gap_us > gap_limit_us:
                continue
            pairs.append(ConcomitantPair(
                entity_a=anchor.entity, entity_b=partner.entity,
                distance_m=distance_m, time_gap_us=gap_us,
                assertion_a=anchor.assertion_id, assertion_b=partner.assertion_id))

    # The deterministic result order: the pair key itself (assertion_a is always
    # the smaller id, so this is a total order over unordered pairs).
    pairs.sort(key=lambda pair: (pair.assertion_a, pair.assertion_b))
    _LOG.info(f"concomitance: {len(pairs)} pair(s) from {len(candidates)} "
              f"candidate(s) (space<={space_threshold_m} m, "
              f"time<={time_threshold}, max_cells={max_cells})")
    return ResultSet(
        pairs=tuple(pairs),
        space_threshold_m=space_threshold_m,
        time_threshold=time_threshold,
        candidate_count=len(candidates))
