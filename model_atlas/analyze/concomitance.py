"""The spatiotemporal concomitance engine: who was with whom, where and when.

Defines:    the public query ``find_concomitant(store_1, store_2=None, *, group_1,
            group_2, space_threshold_m, time_before, time_after, use_accuracy,
            max_cells, sink, on_progress, should_cancel) -> RunSummary``, the
            ``SpatialStrategy`` seam (cell-and-neighbours vs cap-covering — two routes
            to the same superset, see below), the exact great-circle helper
            ``great_circle_distance_m`` and the level chooser ``s2_level_for_radius``.
            ALL S2 level-walking/covering lives here — the schema stores only the leaf.
Used by:    ``model_atlas.app.concomitance_run`` (the ONLY caller — it opens the
            store views and owns the sink's open/close) and
            tests/analyze/test_concomitance.py.
Depends on: ``model_atlas.core.vocabularies`` (the presence-exclusion link tokens and
            the two criterion vocabularies), ``model_atlas.analyze.concomitance_request``
            (``EntityGroup`` / ``Criterion`` / ``CriterionKind`` / ``MatchMode`` — the
            agreed request contract), ``model_atlas.analyze.concomitance_output``
            (``MatchRow`` / ``MatchSink`` — the agreed output contract),
            ``model_atlas.integrate.spatial`` (``s2_cell_to_stored`` — the ONE signed-
            int64 recipe, imported so build and query agree bit-for-bit) and ``s2sphere``.

The question this answers
-------------------------
Which placements of **group 1** are near which placements of **group 2**, where the
two groups are chosen INDEPENDENTLY, may come from two different cases, and the time
window has a *before* and an *after* side. The two groups make the natural operator
action — "who was with this phone" — expressible: group 1 names the phone, group 2 is
left empty (everything).

The algorithm (no O(n²) main path)
----------------------------------
1. ONE indexed SQL pass per side selects the candidate observations: the group predicate
   applied, presence-only (see below), both coordinates and both time bounds present,
   joined through ``spatial_integration``/``spatial_index``/``timespan_integration``.
2. For each group-1 anchor, a search radius is derived (``space_threshold_m``, widened
   by the anchor's own stated accuracy when ``use_accuracy``), and the S2 cells that
   can hold a reachable partner are computed — by default the anchor's cell at the
   chosen level plus its neighbours (``SpatialStrategy.CELL_NEIGHBOURS``).
3. Each cell becomes one leaf range ``[range_min, range_max]`` in SIGNED storage form
   and drives an indexed ``WHERE s2_cell_id BETWEEN ? AND ?`` scan against **group 2's
   store**, pre-filtered in SQL by the temporal window and by the responsibility
   predicate (below) so each pair is scanned exactly once.
4. The survivors are filtered EXACTLY: great-circle distance and the directional
   interval window, both widened by the stated accuracies when ``use_accuracy``. Every
   region scanned in step 3 is a SUPERSET of what step 4 accepts, so step 3 can only
   over-select — never miss.
5. Each surviving pair is streamed to ``sink.write(...)`` immediately. A run over two
   large groups can produce far more rows than either input, so nothing is accumulated.

Why cell-and-neighbours is lossless (the property the whole strategy rests on)
------------------------------------------------------------------------------
For a search radius ``r`` the level is the FINEST whose ``MIN_WIDTH`` >= ``r``
(``s2_level_for_radius``). Every cell at that level is therefore at least ``r`` across,
so a point within ``r`` of the anchor cannot be further away than one cell: it lies in
the anchor's own cell or in one sharing an edge/corner with it. Taking that cell plus
``get_all_neighbors`` is thus a superset of the exact filter's disc.

**The metric choice is load-bearing.** ``MIN_WIDTH`` is a guaranteed minimum;
``AVG_EDGE`` is an AVERAGE, so a below-average cell at that level is NARROWER than
``r`` and a true partner falls outside the 3x3 block — a silent loss that appears only
at some radii. Measured over 6 000 random anchor/partner pairs per radius, MIN_WIDTH
misses nothing at 100 m / 1 000 m / 5 000 m while AVG_EDGE misses at 1 000 m. Never
substitute ``AVG_EDGE``, ``AVG_AREA``, or a hardcoded level->metres table;
``test_min_width_is_a_superset_and_avg_edge_is_not`` pins this.

Accuracy: per-point on the spatial axis, global maximum on the temporal axis
----------------------------------------------------------------------------
All accuracy columns are nullable and a NULL is read as 0 — a point with no stated
uncertainty is taken at face value.

*Spatial.* When both groups select the SAME population (the symmetric case — both
groups empty, or identical selections in one store), each anchor searches
``space_threshold_m + 2 * its own accuracy``. A true match satisfies
``d <= threshold + acc(a) + acc(b)`` and ``acc(a) + acc(b) <= 2*max(acc(a), acc(b))``,
so the FUZZIER point always reaches the other: the pair is found from at least one
side. Each point therefore pays only for its own uncertainty, and one 5 km cell tower
never drags every other anchor's search wide. The ``responsibility`` predicate below
then guarantees that the side which can see the pair is exactly the side that scans it.

That per-point rule is lossless ONLY in the symmetric case, and this engine corrects
it for the general one: with two INDEPENDENT groups (T102's whole point) only group-1
members ever anchor, so a fuzzier group-2 partner never gets its turn to reach back —
a 5 km-accuracy anchor and a 10 km-accuracy partner 12 km apart is a true match that
neither side would scan. Whenever the two populations differ, the partner term
therefore falls back to the GLOBAL maximum group-2 accuracy: the same conservative
bound already taken on the temporal axis, and the only one demonstrably lossless here.
See ``_anchor_radius_m``.

*Temporal.* The same per-point trick applied to BOTH axes loses matches, because the
two uncertainties can sit on DIFFERENT points: with threshold and window both 0,
``a`` fuzzy in space only (10) and ``b`` fuzzy in time only (10), at ``d = 8`` and
``gap = 8``, anchoring at ``a`` reaches spatially but not temporally and anchoring at
``b`` the reverse — neither side finds a true match. So the temporal axis is widened by
the GLOBAL maximum partner time accuracy instead. This costs nothing worth measuring:
the temporal predicate is a ``BETWEEN`` on integer µs already in the WHERE clause, so
widening it does not change the index strategy — unlike the spatial radius, which
decides the S2 level and therefore how much of the index is walked. Spend the per-point
precision where it is expensive; take the cheap conservative bound where it is not.
``test_split_accuracy_axes_counterexample_is_found`` is the named regression test.

Visiting each pair exactly once (the responsibility predicate)
--------------------------------------------------------------
The visit set is every (anchor in group 1 of store 1, partner in group 2 of store 2)
ordered pair. A pair is visited TWICE only when both endpoints qualify both ways —
i.e. the partner also matches group 1 AND the anchor also matches group 2, in ONE
store. Only then does a tie-break apply:

    an anchor is responsible for a partner when the partner's spatial accuracy is
    SMALLER, or equal with a greater observation_id.

The responsible side always reaches: responsibility means ``acc(anchor) >=
acc(partner)``, so its radius ``threshold + 2*acc(anchor) >= threshold + acc(anchor) +
acc(partner) >= d`` for any true match. So exactly one visit exists, it is the one that
can see the pair, and no seen-set, streaming dedup or caching is needed anywhere. With
``use_accuracy=False`` every accuracy term is the literal 0, the first disjunct is never
true and the predicate collapses to ``a.observation_id > :anchor_id`` — one code path,
both behaviours.

WHY the tie-break is CONDITIONAL on the pair being reachable both ways (this is the
one place the engine departs from a literal reading of the task): when the groups are
DISJOINT, only one visit exists, and applying ``observation_id > anchor`` unconditionally
would discard that single visit whenever the partner's id happens to sort lower —
silently losing half of all cross-group matches, including the task's own motivating
case (group 1 = one identifier, group 2 = empty). The brute-force parity tests fail
loudly on the unconditional form.

Presence exclusion (the worked examples' Q3/Q4 split)
-----------------------------------------------------
"Who was together" is a PRESENCE question, so three link tokens exclude a candidate:
``entity_position_link = 'references'`` (a pin/search points at a place — the entity
was never there) and ``never_during_interval`` on EITHER the entity-time or the
spatial-temporal edge (both read as negation — negation never reads as presence). A
NULL link is NOT excluded: unknown is not non-presence, and silently dropping unmapped
rows would hide evidence (process everything by default).

Same identity: one store excludes, two stores do not (deliberate inversion)
---------------------------------------------------------------------------
Within ONE case, two observations carrying the same normalised
``(identifier_type, identifier)`` are never paired: one phone seen by two sources is
convergence, not concomitance, and "where does this one thing keep appearing" is a
hotspot question, not this one. Across TWO cases the exclusion is LIFTED: the same IMEI
in case 1 and case 2 at the same place and time is precisely the finding. The inversion
is intentional — do not "fix" it into consistency. NULL identifiers stay pairable
either way (two unmarked devices may genuinely be two devices).

Determinism
-----------
Anchors are iterated in ``observation_id`` order and each anchor's partners are returned
in ``observation_id`` order, so the emitted stream is ordered by
``(anchor observation_id, partner observation_id)`` and the same request over the same
case(s) produces a byte-identical file.

In the symmetric single-store case the anchor is always the smaller id AND the two
orientations of a pair are interchangeable, so that order coincides exactly with the
old engine's ``(observation_a, observation_b)`` sort — produced WITHOUT buffering, which a
final sort would have required. With an ASYMMETRIC window the coincidence stops: a pair
that satisfies the window only the other way round is emitted partner-first, because
its ``time_delta_s`` must describe the orientation that actually matched. The stream
stays fully deterministic either way.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Protocol

import s2sphere

from model_atlas.analyze.concomitance_output import MatchRow, MatchSink
from model_atlas.analyze.concomitance_request import (
    Criterion,
    CriterionKind,
    EntityGroup,
    MatchMode,
)
from model_atlas.core import vocabularies as V
from model_atlas.integrate.spatial import s2_cell_to_stored

_LOG = logging.getLogger(__name__)

# Mean Earth radius (IUGG 1980 mean radius R1, metres). WHY this constant: S2 works
# on a unit sphere, so the searched region (step 2/3) AND the exact great-circle
# filter (step 4) must use the SAME sphere — one constant guarantees the region can
# never under-cover relative to the filter. WGS84 great-circle arcs differ from this
# sphere by < 0.6 %, well inside any concomitance threshold's intent; the constant is
# documented so the tolerance is auditable.
EARTH_RADIUS_M = 6_371_008.8

# The cap coverer's cell budget: more cells = a tighter covering (fewer false
# candidates to reject exactly) but more BETWEEN scans per anchor. 8 is the s2sphere
# default. Used ONLY by SpatialStrategy.CAP_COVERING.
DEFAULT_MAX_CELLS = 8

# S2 levels run 0..30. Level 0 cells are ~6 007 km across (MIN_WIDTH), so a search
# radius above that has no qualifying level and the spatial restriction is dropped
# entirely rather than under-covered — see s2_level_for_radius.
_S2_MIN_LEVEL = 0
_S2_MAX_LEVEL = 30

# --- the presence-exclusion tokens (from the vocabularies, never re-typed) ---
_REFERENCES_TOKEN = V.EntityPositionLink.REFERENCES.value
_NEVER_ET_TOKEN = V.EntityTimeLink.NEVER_DURING_INTERVAL.value
_NEVER_ST_TOKEN = V.SpatialTemporalLink.NEVER_DURING_INTERVAL.value

# Which vocabulary validates which criterion kind. An identifier is free text by
# nature and stays unvalidated; the two token kinds must be checked (see _tokens).
_KIND_VOCABULARY: dict[CriterionKind, type[Enum] | None] = {
    CriterionKind.ENTITY_TYPE: V.EntityType,
    CriterionKind.IDENTIFIER_TYPE: V.IdentifierType,
    CriterionKind.IDENTIFIER: None,
}

# Which entity column each criterion kind constrains.
_KIND_COLUMN: dict[CriterionKind, str] = {
    CriterionKind.ENTITY_TYPE: "e.type",
    CriterionKind.IDENTIFIER_TYPE: "e.identifier_type",
    CriterionKind.IDENTIFIER: "e.identifier",
}


# --- the public output shapes ---------------------------------------------------

@dataclass(frozen=True)
class RunSummary:
    """What one ``find_concomitant`` call did — the counts a shell reports.

    ``candidates_examined`` is the number of (anchor, partner) pairs that reached the
    EXACT filter, i.e. the work the S2 index did not manage to skip. It exists so the
    two ``SpatialStrategy`` options can be compared on evidence rather than observation
    (see the strategy benchmark in tests/analyze/test_concomitance.py); it never
    affects membership.
    """

    match_count: int
    candidate_count_1: int
    candidate_count_2: int
    cancelled: bool = False
    candidates_examined: int = 0


class SpatialStrategy(Enum):
    """How an anchor's searched region is turned into S2 leaf ranges.

    Both options are supersets of the exact filter's disc and therefore return the
    IDENTICAL match set (pinned by ``test_both_strategies_agree_on_the_random_fixture``);
    only their cost differs, which is why the choice sits behind this one seam.

    ``CELL_NEIGHBOURS`` — the anchor's cell at the level derived from the radius, plus
    ``get_all_neighbors``. ~9 loose cells, no per-anchor covering computation, and the
    accuracy->level mapping falls out naturally.
    ``CAP_COVERING`` — the previous engine's ``Cap`` + ``RegionCoverer``: <= max_cells
    tighter cells, at the cost of a pure-Python covering computation per anchor.
    """

    CELL_NEIGHBOURS = "cell_neighbours"
    CAP_COVERING = "cap_covering"


DEFAULT_STRATEGY = SpatialStrategy.CELL_NEIGHBOURS


class _StoreView(Protocol):
    """The minimal read view this module needs: a live sqlite3 connection.

    WHY a Protocol and not ``RunStore``: the app layer hands in read-only connections
    opened from evidence files, and this module must never open or write a database.
    """

    @property
    def connection(self) -> sqlite3.Connection: ...


# --- exact geometry ------------------------------------------------------------

def great_circle_distance_m(lat_a: float, lon_a: float,
                            lat_b: float, lon_b: float) -> float:
    """Great-circle distance between two WGS84 points, in metres.

    HOW: the haversine formula on the mean-radius sphere (``EARTH_RADIUS_M``) —
    numerically stable for the SHORT arcs concomitance cares about (the naive
    spherical-law-of-cosines loses precision below ~1 m separations).
    WHY this sphere and not the WGS84 ellipsoid: the searched region lives on the
    same sphere, so filter and region agree exactly (see the module docstring).
    """
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    h = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2.0) ** 2)
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


# --- the S2 level choice (ALL level-walking lives here) --------------------------

def s2_level_for_radius(radius_m: float) -> int | None:
    """The finest S2 level whose cells are guaranteed at least ``radius_m`` across.

    HOW: walk levels finest-to-coarsest and return the first whose ``MIN_WIDTH`` (the
    GUARANTEED minimum cell width at that level, scaled to metres on the engine's
    sphere) is at least ``radius_m``. Returns None when even level 0 is narrower than
    the radius.

    WHY MIN_WIDTH and never AVG_EDGE: the cell-and-neighbours block is a superset only
    because EVERY cell at the level is at least ``r`` across. An average admits
    narrower cells, and a partner just outside a narrow cell's 3x3 block is then
    silently lost — measurably so at r = 1 000 m. See the module docstring.

    WHY None rather than clamping to level 0: at a radius wider than a face cell the
    block would no longer cover the disc, so the caller drops the spatial restriction
    and scans every partner instead. Over-selecting is always allowed; under-covering
    is the one thing that must never happen.
    """
    for level in range(_S2_MAX_LEVEL, _S2_MIN_LEVEL - 1, -1):
        if s2sphere.MIN_WIDTH.get_value(level) * EARTH_RADIUS_M >= radius_m:
            return level
    return None


def _cell_block_ranges(latitude: float, longitude: float,
                       radius_m: float) -> list[tuple[int, int]] | None:
    """Signed leaf ranges for the anchor's cell + its neighbours at the chosen level.

    HOW: take the anchor's LEAF cell, truncate it to the chosen level with
    ``parent(level)`` (a pure bit-truncation, free), and add every neighbour the
    library reports. Each resulting cell's descendants form the contiguous leaf range
    ``[range_min, range_max]``, converted to storage form by the ONE imported signing
    helper. None means "no spatial restriction" (see ``s2_level_for_radius``).

    WHY the neighbours come from the library and never from hand-rolled i/j
    arithmetic: a cell at one of the cube's 8 corners has SEVEN neighbours, not eight,
    and face-crossing cells wrap in ways only ``get_all_neighbors`` gets right.
    API gotcha: ``get_all_neighbors(level)`` asserts ``level >= self.level()``, and it
    returns a GENERATOR — so the observation fires on ITERATION, not on the call. It must
    therefore be called on the parent at the chosen level, never on the level-30 leaf.
    """
    level = s2_level_for_radius(radius_m)
    if level is None:
        return None
    leaf = s2sphere.CellId.from_lat_lng(
        s2sphere.LatLng.from_degrees(latitude, longitude))
    parent = leaf.parent(level)
    cells = [parent, *parent.get_all_neighbors(level)]
    return [(s2_cell_to_stored(cell.range_min().id()),
             s2_cell_to_stored(cell.range_max().id()))
            for cell in cells]


def _cap_cell_ranges(latitude: float, longitude: float, radius_m: float,
                     max_cells: int) -> list[tuple[int, int]] | None:
    """Signed leaf ranges covering a cap of ``radius_m`` around a point.

    HOW: cap radius in radians = arc/R on the SAME sphere as the exact filter;
    ``RegionCoverer`` (default level bounds 0..30, ``max_cells`` capped) returns a
    small covering; each covering cell's descendants are the contiguous leaf range.
    WHY the signed form is BETWEEN-safe: a cell's range never crosses a face (top-bit)
    boundary, so both bounds keep their relative order (see the same signing contract
    in ``integrate/spatial.py``).
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


def _ranges_for(strategy: SpatialStrategy, latitude: float, longitude: float,
                radius_m: float, max_cells: int) -> list[tuple[int, int]] | None:
    """The one seam: an anchor's searched region as signed leaf ranges (None = all)."""
    if strategy is SpatialStrategy.CELL_NEIGHBOURS:
        return _cell_block_ranges(latitude, longitude, radius_m)
    return _cap_cell_ranges(latitude, longitude, radius_m, max_cells)


# --- the candidate row (private working shape) ----------------------------------

@dataclass(frozen=True)
class _Candidate:
    """One candidate observation: identity, integrated point, canonical interval and the
    stated uncertainties, plus the rendered strings a result row carries.

    Every accuracy field is already NULL-collapsed to 0 by the loader — a point with
    no stated uncertainty is taken at face value.
    """

    observation_id: str
    identifier: str | None
    identifier_type: str
    entity_type: str
    latitude: float
    longitude: float
    accuracy_m: float | None      # as stated (None survives into the output row)
    lower_us: int
    upper_us: int
    lower_accuracy_us: int
    upper_accuracy_us: int
    time_start: str | None
    time_end: str | None
    source: str | None

    @property
    def spatial_accuracy_m(self) -> float:
        """The stated horizontal accuracy with NULL read as 0 (face value)."""
        return self.accuracy_m or 0.0


# --- SQL assembly ----------------------------------------------------------------
# One shared FROM/WHERE for BOTH the candidate load and the per-anchor range scans, so
# anchor and partner are drawn from the IDENTICAL population apart from the group
# clause — an asymmetry here (e.g. the partner path requiring an index row the
# candidate path did not) would silently lose pairs. The spatial_index join is part of
# the candidate definition: spatial integration writes one index row per integrated
# point, so a point without one is not a queryable point.

_FROM_JOINS = (
    " FROM observation a"
    " JOIN entity e ON e.entity_id = a.entity_id"
    " JOIN spatial_integration si"
    "   ON si.spatial_expression_id = a.spatial_expression_id"
    " JOIN spatial_index six"
    "   ON six.spatial_integration_id = si.spatial_integration_id"
    " JOIN timespan_integration ti"
    "   ON ti.temporal_expression_id = a.temporal_expression_id"
)

# The presence + completeness predicate. SQLite's ``IS NOT`` is the NULL-safe
# inequality, so a NULL link SURVIVES each exclusion (unknown is not non-presence — a
# plain ``!=`` would silently drop every unmapped row). The coordinate null checks
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

# Every column a _Candidate needs. Selected by the candidate load only; the per-anchor
# scan selects just the observation_id and looks the rest up in the preloaded dict.
_CANDIDATE_COLUMNS = (
    "SELECT a.observation_id, e.identifier, e.identifier_type, e.type,"
    " si.latitude_wgs84, si.longitude_wgs84, si.horizontal_accuracy_m,"
    " ti.lower_unix_utc_us, ti.upper_unix_utc_us,"
    " ti.lower_accuracy_us, ti.upper_accuracy_us,"
    " ti.lower_datetime_iso, ti.upper_datetime_iso, a.source_label"
)


def _tokens(criteria: Iterable[Criterion], kind: CriterionKind) -> tuple[str, ...]:
    """The stored tokens of one criterion kind, validated against its vocabulary.

    WHY validate: a typo'd type ('vehicel') would otherwise match nothing and return an
    EMPTY result that reads as 'no concomitance found' — the classic silent loss;
    raising at the boundary keeps the answer honest. Identifiers are free text by
    nature and are deliberately NOT validated (T101a's ``case_vocabulary`` already
    limits the GUI to values present in the case).
    """
    rendered = tuple(str(getattr(c.value, "value", c.value))
                     for c in criteria if c.kind is kind)
    enum_type = _KIND_VOCABULARY[kind]
    if enum_type is not None and rendered:
        allowed = {member.value for member in enum_type}
        unknown = sorted(set(rendered) - allowed)
        if unknown:
            raise ValueError(
                f"entity group {kind.value} contains unknown tokens {unknown}; "
                f"allowed: {sorted(allowed)}")
    return rendered


def _group_clause(group: EntityGroup) -> tuple[str, list[str]]:
    """One group's SQL predicate (without a leading AND) and its bind parameters.

    HOW: criteria of the SAME kind always collapse to one ``IN (...)``; ``MatchMode.ANY``
    then OR-s the per-kind clauses, ``MatchMode.ALL`` AND-s them. An empty group returns
    an empty predicate — no constraint at all, because narrowing is opt-in (house rule).
    """
    per_kind: list[str] = []
    params: list[str] = []
    for kind in CriterionKind:
        values = _tokens(group.criteria, kind)
        if not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        per_kind.append(f"{_KIND_COLUMN[kind]} IN ({placeholders})")
        params.extend(values)
    if not per_kind:
        return "", []
    joiner = " OR " if group.mode is MatchMode.ANY else " AND "
    return "(" + joiner.join(per_kind) + ")", params


def _load_candidates(connection: sqlite3.Connection,
                     group: EntityGroup) -> dict[str, _Candidate]:
    """Every candidate observation for one group, keyed by observation_id, in id order.

    One indexed pass; the ORDER BY makes the anchor iteration — and therefore the
    emitted stream — deterministic without buffering the results.
    """
    clause, params = _group_clause(group)
    group_sql = f" AND {clause}" if clause else ""
    rows = connection.execute(
        _CANDIDATE_COLUMNS + _FROM_JOINS + _PRESENCE_WHERE + group_sql
        + " ORDER BY a.observation_id",
        (*_PRESENCE_PARAMS, *params))
    candidates: dict[str, _Candidate] = {}
    for (observation_id, identifier, identifier_type, entity_type,
         latitude, longitude, accuracy_m, lower_us, upper_us,
         lower_acc_us, upper_acc_us, time_start, time_end, source) in rows:
        candidates[observation_id] = _Candidate(
            observation_id=observation_id,
            identifier=identifier, identifier_type=identifier_type,
            entity_type=entity_type,
            latitude=latitude, longitude=longitude, accuracy_m=accuracy_m,
            lower_us=lower_us, upper_us=upper_us,
            # NULL accuracy = 0: a bound with no stated uncertainty is taken at face
            # value (documented in the module docstring).
            lower_accuracy_us=lower_acc_us or 0,
            upper_accuracy_us=upper_acc_us or 0,
            time_start=time_start, time_end=time_end, source=source)
    return candidates


def _max_time_accuracy_us(candidates: dict[str, _Candidate]) -> int:
    """The GLOBAL maximum stated time accuracy across a candidate population.

    WHY a global maximum and not a per-point value: see the split-axis counterexample
    in the module docstring — a per-point temporal rule loses true matches whose
    spatial and temporal uncertainties sit on different points.
    """
    return max((max(c.lower_accuracy_us, c.upper_accuracy_us)
                for c in candidates.values()), default=0)


def _max_spatial_accuracy_m(candidates: dict[str, _Candidate]) -> float:
    """The GLOBAL maximum stated horizontal accuracy across a candidate population.

    Only consulted when the two groups do NOT select the same population — see
    ``_anchor_radius_m`` for why that case cannot use the per-point rule.
    """
    return max((c.spatial_accuracy_m for c in candidates.values()), default=0.0)


def _anchor_radius_m(anchor: _Candidate, space_threshold_m: float, *,
                     use_accuracy: bool, reciprocal: bool,
                     max_partner_accuracy_m: float) -> float:
    """How far one anchor must search for its partners — always a lossless bound.

    A true match satisfies ``d <= threshold + acc(a) + acc(b)``, so an anchor reaches
    every partner it is responsible for as long as its radius covers that sum.

    ``reciprocal`` (both groups select the SAME population, so every pair can be
    visited from either end) is the case the per-point rule was designed for: since
    ``acc(a) + acc(b) <= 2*max(acc(a), acc(b))``, searching ``threshold + 2*acc(a)``
    means the FUZZIER endpoint always reaches the other, and the responsibility
    predicate makes that fuzzier endpoint the one that scans. Each point then pays only
    for its own uncertainty and one 5 km cell tower never drags every other anchor's
    search wide.

    WHY that rule is NOT lossless for two INDEPENDENT groups (the correction this
    engine makes to the per-point design): when the groups are disjoint, only group-1
    members ever anchor, so a fuzzier group-2 partner never gets its turn to reach
    back. A 5 km-accuracy anchor and a 10 km-accuracy partner 12 km apart is a true
    match that the anchor's 10 km reach cannot see and that nothing else will scan —
    a silent loss. So whenever the populations differ, the partner term falls back to
    the GLOBAL maximum group-2 accuracy: exactly the conservative bound already taken
    on the temporal axis, and the only bound that can be shown lossless here.
    """
    if not use_accuracy:
        return space_threshold_m
    partner_term = (anchor.spatial_accuracy_m if reciprocal
                    else max_partner_accuracy_m)
    return space_threshold_m + anchor.spatial_accuracy_m + partner_term


# --- the exact filters -------------------------------------------------------------

def _signed_gap_us(anchor: _Candidate, partner: _Candidate) -> tuple[int, bool]:
    """The SIGNED gap between two closed intervals in µs, and whether they overlap.

    HOW: positive when ``partner`` is LATER than ``anchor`` (partner starts after the
    anchor ends), negative when EARLIER, 0 when the intervals overlap or touch. Exact
    integer arithmetic — no float µs. The sign is what carries the direction into
    ``time_delta_s``.
    """
    if partner.lower_us > anchor.upper_us:
        return partner.lower_us - anchor.upper_us, False
    if anchor.lower_us > partner.upper_us:
        return -(anchor.lower_us - partner.upper_us), False
    return 0, True


def _within_window(anchor: _Candidate, partner: _Candidate,
                   before_us: int, after_us: int, use_accuracy: bool) -> bool:
    """Whether ``partner`` falls in ``anchor``'s directional window.

    The window is asymmetric and defined RELATIVE TO THE ANCHOR (the group-1 side):
    the partner may be up to ``after_us`` later or ``before_us`` earlier. With
    ``use_accuracy`` each side of the gap is relaxed by the two bounds that actually
    face each other — the anchor's upper and the partner's lower accuracy when the
    partner is later, and the mirror pair when it is earlier.
    """
    gap_us, overlap = _signed_gap_us(anchor, partner)
    if overlap:
        return True
    if gap_us > 0:
        slack = (anchor.upper_accuracy_us + partner.lower_accuracy_us
                 if use_accuracy else 0)
        return gap_us - slack <= after_us
    slack = (anchor.lower_accuracy_us + partner.upper_accuracy_us
             if use_accuracy else 0)
    return -gap_us - slack <= before_us


def _same_identity(a: _Candidate, b: _Candidate) -> bool:
    """True when both sides carry the SAME normalised working identity.

    NULL identifiers are never 'the same' (two unmarked devices may genuinely be two
    devices). Only consulted in the single-store case — see the module docstring's
    deliberate one-store/two-store inversion.
    """
    return (a.identifier is not None
            and a.identifier == b.identifier
            and a.identifier_type == b.identifier_type)


def _to_match_row(g1: _Candidate, g2: _Candidate, distance_m: float,
                  space_threshold_m: float) -> MatchRow:
    """Build the output row for a confirmed match, group-1 side first.

    ``distance_within_accuracy`` records whether the stated accuracies were NEEDED to
    make this pair match — i.e. the exact great-circle distance alone exceeds the
    threshold. That is what lets an analyst separate "they were together" from "they
    were together within the uncertainty of the fix".
    """
    gap_us, overlap = _signed_gap_us(g1, g2)
    return MatchRow(
        g1_identifier=g1.identifier,
        g1_identifier_type=g1.identifier_type,
        g1_entity_type=g1.entity_type,
        g1_latitude=g1.latitude, g1_longitude=g1.longitude,
        g1_accuracy_m=g1.accuracy_m,
        g1_time_start=g1.time_start, g1_time_end=g1.time_end,
        g1_observation_id=g1.observation_id, g1_source=g1.source,
        g2_identifier=g2.identifier,
        g2_identifier_type=g2.identifier_type,
        g2_entity_type=g2.entity_type,
        g2_latitude=g2.latitude, g2_longitude=g2.longitude,
        g2_accuracy_m=g2.accuracy_m,
        g2_time_start=g2.time_start, g2_time_end=g2.time_end,
        g2_observation_id=g2.observation_id, g2_source=g2.source,
        distance_m=distance_m,
        distance_within_accuracy=distance_m > space_threshold_m,
        time_delta_s=gap_us / 1_000_000.0,
        time_overlap=overlap)


# --- the public query --------------------------------------------------------------

def find_concomitant(
    store_1: _StoreView,
    store_2: _StoreView | None = None,
    *,
    group_1: EntityGroup,
    group_2: EntityGroup,
    space_threshold_m: float,
    time_before: dt.timedelta,
    time_after: dt.timedelta,
    use_accuracy: bool = False,
    max_cells: int = DEFAULT_MAX_CELLS,
    sink: MatchSink,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    strategy: SpatialStrategy = DEFAULT_STRATEGY,
) -> RunSummary:
    """Stream every group-1/group-2 placement pair inside both thresholds.

    ``store_2=None`` draws both groups from ``store_1`` (and applies the same-identity
    exclusion); a distinct ``store_2`` compares two cases (and lifts it). Anchors stream
    from ``store_1``; each anchor's range scan runs against ``store_2``'s connection,
    because each case has its own ``spatial_index`` and a cross-database range scan
    could not use one index.

    ``time_before``/``time_after`` are how much EARLIER/LATER group 2 may be than
    group 1. ``use_accuracy`` widens both axes by the stated uncertainties.
    ``max_cells`` tunes ``SpatialStrategy.CAP_COVERING`` only — a larger budget tightens
    the covering, it never changes the result (the exact filter decides membership).

    Matches go to ``sink.write(...)`` as they are found; the CALLER owns the sink's
    ``open``/``close`` (this module performs no I/O of its own and opens no database).
    ``on_progress(done, total)`` and ``should_cancel()`` are both called once per
    anchor. On cancel the loop stops and a ``RunSummary`` with ``cancelled=True`` and
    the partial count is returned — a forensic tool never discards completed work
    silently.
    """
    # Invariant guards: a negative threshold or window has no geometric/temporal
    # meaning — proceeding would silently return an empty result that reads as
    # 'nothing found' rather than 'you asked an impossible question'.
    if space_threshold_m < 0:
        raise ValueError(f"space_threshold_m must be >= 0, got {space_threshold_m}")
    if time_before < dt.timedelta(0):
        raise ValueError(f"time_before must be >= 0, got {time_before}")
    if time_after < dt.timedelta(0):
        raise ValueError(f"time_after must be >= 0, got {time_after}")
    if max_cells < 1:
        raise ValueError(f"max_cells must be >= 1, got {max_cells}")

    # timedelta floor-division by 1 µs is EXACT (timedelta stores integer µs);
    # total_seconds() would round through a float.
    before_us = time_before // dt.timedelta(microseconds=1)
    after_us = time_after // dt.timedelta(microseconds=1)

    one_store = store_2 is None or store_2 is store_1
    connection_1 = store_1.connection
    connection_2 = connection_1 if one_store else store_2.connection

    anchors = _load_candidates(connection_1, group_1)
    partners = _load_candidates(connection_2, group_2)
    # The global temporal widening (see the module docstring's split-axis rule). Zero
    # when accuracy is off, so the window arithmetic below has one code path.
    max_partner_time_acc_us = (
        _max_time_accuracy_us(partners) if use_accuracy else 0)
    # Reciprocal = the two groups select the SAME population in ONE store, so every
    # pair can be visited from either end. That is the ONLY situation in which the
    # cheap per-point spatial rule is lossless — see _anchor_radius_m.
    reciprocal = one_store and anchors.keys() == partners.keys()
    max_partner_accuracy_m = (
        _max_spatial_accuracy_m(partners) if use_accuracy else 0.0)

    group_2_clause, group_2_params = _group_clause(group_2)
    group_1_clause, group_1_params = _group_clause(group_1)
    # Does the PARTNER row also satisfy group 1? Needed to know whether this pair is
    # reachable from both sides and therefore needs a tie-break at all. An empty
    # group 1 matches everything, hence the literal 1.
    partner_in_group_1_sql = group_1_clause or "1"

    # WHY the accuracy term is a LITERAL 0 when use_accuracy is off, rather than the
    # column with a 0 bind: with the column, a partner carrying a stated accuracy
    # would fail BOTH disjuncts of the tie-break against a zero-accuracy anchor and
    # the pair would be silently suppressed. The literal keeps the predicate exactly
    # equal to the old engine's ``a.observation_id > ?`` — one code path, both behaviours.
    partner_accuracy_sql = (
        "COALESCE(si.horizontal_accuracy_m, 0)" if use_accuracy else "0")

    scan_sql_head = (
        "SELECT a.observation_id" + _FROM_JOINS + _PRESENCE_WHERE
        + (f" AND {group_2_clause}" if group_2_clause else ""))

    match_count = 0
    examined = 0
    cancelled = False
    total = len(anchors)
    done = 0

    for anchor in anchors.values():
        if should_cancel is not None and should_cancel():
            cancelled = True
            break

        radius_m = _anchor_radius_m(
            anchor, space_threshold_m, use_accuracy=use_accuracy,
            reciprocal=reciprocal, max_partner_accuracy_m=max_partner_accuracy_m)
        ranges = _ranges_for(strategy, anchor.latitude, anchor.longitude,
                             radius_m, max_cells)

        # The COARSE temporal cut. It must widen to max(before, after) on BOTH sides:
        # when the groups overlap, the single visit tests both orientations, so a
        # partner that only satisfies the window the other way round must still be
        # scanned. The accuracy terms are the anchor's own slack plus the global
        # partner maximum, both 0 when accuracy is off.
        window_us = max(before_us, after_us)
        slack_us = (max(anchor.lower_accuracy_us, anchor.upper_accuracy_us)
                    + max_partner_time_acc_us)
        upper_limit = anchor.upper_us + window_us + slack_us
        lower_limit = anchor.lower_us - window_us - slack_us

        params: list[object] = [*_PRESENCE_PARAMS, *group_2_params]
        sql = scan_sql_head
        if ranges is not None:
            sql += " AND (" + " OR ".join(
                "six.s2_cell_id BETWEEN ? AND ?" for _ in ranges) + ")"
            params.extend(bound for cell_range in ranges for bound in cell_range)
        sql += " AND ti.lower_unix_utc_us <= ? AND ti.upper_unix_utc_us >= ?"
        params.extend((upper_limit, lower_limit))

        # The responsibility predicate — the ONLY dedup mechanism (no seen-set, no
        # streaming dedup, no extra memory). It applies ONLY when the pair is
        # genuinely reachable from both sides, i.e. in ONE store with the anchor also
        # in group 2 and the partner also in group 1; otherwise a single visit exists
        # and suppressing it would lose the match outright. See the module docstring.
        anchor_in_group_2 = one_store and anchor.observation_id in partners
        if anchor_in_group_2:
            sql += (f" AND (NOT ({partner_in_group_1_sql})"
                    f" OR {partner_accuracy_sql} < ?"
                    f" OR ({partner_accuracy_sql} = ? AND a.observation_id > ?))")
            params.extend(group_1_params)
            params.extend((anchor.spatial_accuracy_m if use_accuracy else 0,
                           anchor.spatial_accuracy_m if use_accuracy else 0,
                           anchor.observation_id))

        # ORDER BY keeps each anchor's emissions deterministic, so the streamed file
        # is byte-identical across runs without ever buffering the results.
        sql += " ORDER BY a.observation_id"

        for (partner_id,) in connection_2.execute(sql, params).fetchall():
            partner = partners.get(partner_id)
            # Invariant: the scan's FROM/WHERE equals the candidate load's apart from
            # the group clause, which the scan also applies — so every scanned id MUST
            # be a loaded group-2 candidate. A miss means the two predicates drifted
            # apart (a lost-pair bug), never a normal outcome.
            assert partner is not None, (
                f"partner {partner_id} matched the range scan but is not a "
                f"candidate: the scan and load predicates drifted")
            if one_store and partner.observation_id == anchor.observation_id:
                continue          # an observation is never concomitant with itself
            if one_store and _same_identity(anchor, partner):
                continue          # convergence, not concomitance (see the docstring)

            examined += 1
            distance_m = great_circle_distance_m(
                anchor.latitude, anchor.longitude,
                partner.latitude, partner.longitude)
            reach_m = space_threshold_m + (
                anchor.spatial_accuracy_m + partner.spatial_accuracy_m
                if use_accuracy else 0.0)
            if distance_m > reach_m:
                continue

            # Both orientations are tested on this single visit (the pair is unordered
            # — "a match is a match"), but only an orientation whose group-1 side
            # really is in group 1 is meaningful. The anchor orientation is preferred
            # when both hold, which keeps the emitted g1/g2 assignment deterministic.
            if _within_window(anchor, partner, before_us, after_us, use_accuracy):
                row = _to_match_row(anchor, partner, distance_m, space_threshold_m)
            elif (partner_id in anchors and anchor_in_group_2
                  and _within_window(partner, anchor, before_us, after_us,
                                     use_accuracy)):
                row = _to_match_row(partner, anchor, distance_m, space_threshold_m)
            else:
                continue

            sink.write(row)
            match_count += 1

        done += 1
        if on_progress is not None:
            on_progress(done, total)

    _LOG.info(f"concomitance: {match_count} match(es) from {len(anchors)} group-1 and "
              f"{len(partners)} group-2 candidate(s) "
              f"(space<={space_threshold_m} m, before<={time_before}, "
              f"after<={time_after}, accuracy={use_accuracy}, "
              f"strategy={strategy.value}, cancelled={cancelled})")
    return RunSummary(
        match_count=match_count,
        candidate_count_1=len(anchors),
        candidate_count_2=len(partners),
        cancelled=cancelled,
        candidates_examined=examined)
