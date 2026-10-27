"""Spatial integration Methods (L2) + the S2 leaf index.

Defines:    the spatial ``Method``s of docs/refactor_plan.md §3 — ``PassthroughWgs84``
            (adopts WGS84 latitude/longitude verbatim-text-to-float) and ``UnitConvert``
            (interprets every metric component into its canonical unit via the core
            ``CANONICAL_UNIT`` / ``unit_factor`` tables) — plus the not-yet-available
            stubs ``GeocodeAddress`` / ``GeocodePlaceName`` / ``CellRegisterLookup``;
            the per-expression composer ``integrate_expression`` (one method-stamped
            ``spatial_integration`` row per expression) and the store-level driver
            ``integrate_spatial`` (also writes the ``spatial_index`` S2 leaf row per
            point); and the S2 storage helpers ``s2_cell_to_stored`` /
            ``s2_cell_from_stored`` / ``s2_leaf_cell_id``.
Used by:    the app-layer pipeline and tests/integrate/test_spatial.py. The
            concomitance query brick MUST import ``s2_cell_to_stored`` /
            ``s2_cell_from_stored`` so build and query agree bit-for-bit — a locally
            re-derived signing recipe would silently misalign every range scan.
Depends on: ``model_atlas.core`` (``ids`` — every id derivation, ``vocabularies`` —
            the family dataclasses + the unit tables, ``store`` — the layer-typed
            writers) and the pure-Python ``s2sphere`` package.

The L2 contract (docs/run_db_schema.md §Layer 2)
------------------------------------------------
L1 keeps every value VERBATIM; every conversion happens here, method-stamped: the
``method`` column names rule + version (+ reference data), e.g.
``passthrough_wgs84/1.0`` or ``passthrough_wgs84/1.0+unit_convert/1.0`` — a
pass-through is a method too (*derived value => documented derivation*, no
exceptions). One expression gets AT MOST one integration row in v1
(UNIQUE(spatial_expression_id)): each applicable Method contributes disjoint
columns and its ``name/version`` fragment, and the composer joins the fragments with
``+`` in method-set order. An expression no Method can interpret (an address not yet
geocoded, a gridded zone/easting/northing trio) gets NO integration row — and hence
no ``spatial_index`` row — rather than an empty stamped row claiming a derivation
that never ran.

Region / place-name substitution: when the geocoding Methods are implemented,
an integration standing in a REGION or PLACE-NAME centroid for the true position must
NAME that substitution in the method string (``region_centroid/<register>`` /
``place_centroid/<register>``) — the stub docstrings below carry the rule so the
future implementation cannot miss it.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence

import s2sphere

from model_atlas.core import ids
from model_atlas.core import vocabularies as V
from model_atlas.core.store import RunStore

# --- the S2 storage helpers --------------------------------------------------
# ``CellId.from_lat_lng`` always returns a LEAF cell; s2sphere levels run 0..30.
S2_LEAF_LEVEL = 30


def s2_cell_to_stored(cell_id: int) -> int:
    """The signed-int64 STORAGE form of an unsigned 64-bit S2 cell id.

    HOW: SQLite INTEGER is signed 64-bit while ``CellId.id()`` is uint64, so a value
    with the top bit set (faces 4-5) is reinterpreted as negative
    (``v - 2**64 when v >= 2**63``) — the SAME 64 bits, just signed.
    WHY order still holds for range scans: an S2 cell's ``[range_min, range_max]``
    never crosses a face boundary (the face is the top 3 bits), so both bounds and
    every descendant share the top bit and the signed reinterpretation preserves
    their relative order — ``WHERE s2_cell_id BETWEEN ? AND ?`` stays correct.
    The concomitance queries MUST use this same helper when computing range
    bounds; a second recipe that differed by one bit would silently miss matches.
    """
    return cell_id - 2**64 if cell_id >= 2**63 else cell_id


def s2_cell_from_stored(stored: int) -> int:
    """The unsigned 64-bit S2 cell id back from its signed STORAGE form.

    Exact inverse of ``s2_cell_to_stored`` (negative values get ``2**64`` back), so a
    stored ``s2_cell_id`` read out of SQLite can be handed to ``s2sphere.CellId``
    (which expects the unsigned id) for level-walking in the query engine.
    """
    return stored + 2**64 if stored < 0 else stored


def s2_leaf_cell_id(latitude: float, longitude: float) -> int:
    """The LEAF S2 cell id for a WGS84 point, already in signed storage form.

    One call site per point at build time (below) — and the leaf level is fixed at
    30 (``S2_LEAF_LEVEL``), the finest S2 subdivision, so the index never loses
    precision; coarser grouping is a bit-truncation at query time, not a rebuild.
    """
    cell = s2sphere.CellId.from_lat_lng(s2sphere.LatLng.from_degrees(latitude, longitude))
    return s2_cell_to_stored(cell.id())


# --- the Method input (what one L1 spatial expression looks like to a Method) --

@dataclass(frozen=True)
class SpatialExpressionInput:
    """One L1 spatial expression as the Methods consume it.

    ``part_values`` maps each component ``part`` to its VERBATIM ``raw_value`` text
    (components whose raw_value is NULL carry no value and are omitted). The loader
    guarantees an interpretable part appears at most once (see ``_load_expressions``),
    so a Method can read the mapping without an ambiguity rule of its own.
    """

    spatial_expression_id: str
    referential: str
    # mutable default — shared across instances if = {} were used
    part_values: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------------
    # EVERYTHING BELOW IS KEYWORD-ONLY, and that is a structural guard, not a style
    # choice. The three fields above are the positional form used throughout the tests
    # (``SpatialExpressionInput("sx", "epsg:4326", {"latitude": "46.2"})``). Adding a
    # field ANYWHERE above this line silently rebinds those positional arguments: the
    # part_values dict lands in the new field, the Method sees an expression with no
    # parts, and a "must raise" test stops raising rather than failing loudly — a green
    # suite hiding a broken guard.
    #
    # This happened twice. The first time, a comment was added saying "keep new fields
    # last"; the second time that comment was read and the mistake made anyway. A comment
    # cannot enforce an ordering constraint, so the constraint is now enforced by the
    # language: ``kw_only=True`` makes declaration order irrelevant below this line, and a
    # positional 4th argument is a TypeError at the call site instead of a silent rebind.
    # tests/integrate/test_spatial.py pins this.
    # ------------------------------------------------------------------------

    # part -> the SOURCE COLUMN the value came from. Carried so ``position_raw`` can name
    # the column beside the value, which is what makes an integrated coordinate auditable
    # back to the artefact. Mutable default, same reason as above.
    part_source_fields: dict[str, str] = field(default_factory=dict, kw_only=True)
    spatial_source: str | None = field(default=None, kw_only=True)
    # WHAT KIND of locator this is — the axis Methods dispatch on. Defaults to the type the
    # referential implies, so a caller predating the split still resolves correctly.
    position_type: str | None = field(default=None, kw_only=True)


# The parts the v1 Methods interpret. The two angles are DATUM-AGNOSTIC L1 parts
# (``latitude`` / ``longitude`` — the source's words); adopting them as WGS84 degrees is
# the CRS interpretation (no unit conversion), and it MAPS the agnostic part onto its
# canonical L2 column ``*_wgs84`` via ``_PART_TO_COLUMN`` below. Every metric part is
# exactly the set of columns carrying a canonical unit in core/vocabularies.py, so a
# column added there is picked up here with no second list to update.
_LATITUDE_PART_NAME = "latitude"
_LONGITUDE_PART_NAME = "longitude"
_ANGLE_PARTS: frozenset[str] = frozenset({_LATITUDE_PART_NAME, _LONGITUDE_PART_NAME})
# L1 angle part -> canonical L2 integration column. The datum lives in the referential at
# L1; adopting it names the canonical WGS84 column, so the part->column rename happens
# HERE (the interpretation), never at L1.
_PART_TO_COLUMN: dict[str, str] = {
    _LATITUDE_PART_NAME: "latitude_wgs84",
    _LONGITUDE_PART_NAME: "longitude_wgs84",
}
_METRIC_PARTS: frozenset[str] = frozenset(V.CANONICAL_UNIT)
# The two canonical columns whose derivation ``position_raw`` evidences. Derived from the
# part->column map so a renamed column cannot leave the check silently matching nothing.
_INTERPRETABLE_PARTS: frozenset[str] = _ANGLE_PARTS | _METRIC_PARTS
# The two canonical columns whose derivation ``position_raw`` evidences. Derived from the
# part->column map so a renamed column cannot leave the check silently matching nothing.
_COORDINATE_COLUMNS: frozenset[str] = frozenset(_PART_TO_COLUMN.values())


def _parse_float(part: str, raw_value: str) -> float:
    """The float a verbatim numeric raw_value denotes, or a LOUD ValueError.

    WHY raise rather than skip: a non-numeric text under a numeric part means the L1
    referential/part claim is broken (a preset mapped the wrong column) — silently
    producing a row without that column would hide the bug (no-silent-loss).
    """
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"spatial part {part!r} carries a non-numeric raw value {raw_value!r}; "
            f"the preset mapped a non-numeric source column onto a numeric part") from exc
    # HOW: bare float() also parses 'inf'/'nan'/'infinity' (and underscore/whitespace
    # forms), so a non-finite result must be rejected explicitly. WHY loudly, like the
    # non-numeric case: a non-finite magnitude is bogus evidence — an 'inf' speed would
    # be stamped as a real value, and 'nan'/'inf' under latitude/longitude crashes deep
    # in s2sphere's from_lat_lng observation, aborting integration of every later
    # expression rather than failing this one cleanly.
    if not math.isfinite(value):
        raise ValueError(
            f"spatial part {part!r} carries a non-finite raw value {raw_value!r}; "
            f"a nan/inf value is not a valid coordinate or metric magnitude")
    return value


# --- the Method protocol (docs/refactor_plan.md §3) --------------------------

class SpatialMethod(Protocol):
    """The spatial shape of the Method contract: (expression) -> partial columns.

    ``integrate`` returns the column->value mapping this Method derives (a PARTIAL
    view of the one integration row — the composer merges the method set's disjoint
    contributions), or None when it has nothing. ``name``/``version`` (+ any
    reference data an instance carries) form its stored method-string fragment.

    ``coordinate_inputs`` names the L1 parts this method READ to produce
    ``latitude_wgs84``/``longitude_wgs84``, in the order they should be recorded. The
    composer turns them into ``position_raw`` — the verbatim evidence behind the derived
    coordinate. A method that produces no coordinates returns an empty sequence.

    WHY the method declares it rather than the composer guessing: the inputs differ per
    MECHANISM, and only the method knows its own. A reprojection reads the two angle parts;
    a geocoder reads a postal address; a cell-register or BSSID lookup reads an identifier.
    Hard-coding "the latitude and longitude parts" here would silently produce an EMPTY
    ``position_raw`` the day the first non-reprojection method ships, which is exactly the
    class of silent loss this model exists to prevent.
    """

    name: str
    version: str

    def applies_to(self, expression: SpatialExpressionInput) -> bool: ...

    def integrate(self, expression: SpatialExpressionInput) -> dict[str, float] | None: ...

    def coordinate_inputs(
        self, expression: SpatialExpressionInput) -> Sequence[str]: ...


class PassthroughWgs84:
    """Method ``passthrough_wgs84/1.0``: adopt WGS84 latitude/longitude as floats.

    The pass-through IS a documented derivation (verbatim text -> REAL column), so it
    is stamped like any other method. Only the two angle parts are handled here — the
    CRS adoption is a different interpretation from a unit conversion, so the metric
    columns belong to ``UnitConvert`` and the two method names stay honest. It applies
    ONLY when the referential is ``epsg:4326``: that is what licenses reading the
    datum-agnostic L1 ``latitude`` / ``longitude`` as canonical WGS84 (a preset that maps
    coordinates but states no referential resolves to ``unknown`` and is NOT adopted).
    """

    name = "passthrough_wgs84"
    version = "1.0"

    def applies_to(self, expression: SpatialExpressionInput) -> bool:
        # BOTH conditions: the locator must be a coordinate AND stated in WGS84. The type
        # says a transformation (not a lookup) can reach WGS84; the referential says the
        # components already ARE WGS84 degrees, so adopting them is a pass-through.
        return (expression.position_type == V.PositionType.COORDINATE.value
                and expression.referential == V.SpatialReferential.EPSG_4326.value
                and any(part in expression.part_values for part in _ANGLE_PARTS))

    def integrate(self, expression: SpatialExpressionInput) -> dict[str, float] | None:
        # Map each datum-agnostic L1 angle part onto its canonical L2 column: the returned
        # dict keys are the ``*_wgs84`` integration columns, so the WGS84 datum is named at
        # the point of adoption (here), not baked into the L1 part name.
        columns = {
            _PART_TO_COLUMN[part]: _parse_float(part, expression.part_values[part])
            for part in (_LATITUDE_PART_NAME, _LONGITUDE_PART_NAME)
            if part in expression.part_values
        }
        return columns or None

    def coordinate_inputs(self, expression: SpatialExpressionInput) -> Sequence[str]:
        """The angle parts adopted, in latitude-then-longitude order."""
        return [part for part in (_LATITUDE_PART_NAME, _LONGITUDE_PART_NAME)
                if part in expression.part_values]


class UnitConvert:
    """Method ``unit_convert/1.0``: every metric component into its canonical unit.

    ``source_units`` (part -> the SOURCE unit the preset declared, e.g.
    ``{"horizontal_speed_kmh": "m/s"}``) is this Method's reference data; a part with
    no declared source unit is read as ALREADY canonical (factor 1.0 via the same
    ``unit_factor`` table — never a silent bypass, so an unknown unit still raises).
    WHY the identity case is stamped too: reading the text "50" as 50.0 km/h is
    itself the interpretation — the conversion IS the interpretation, and every
    derived value must name its derivation.
    """

    name = "unit_convert"
    version = "1.0"

    def __init__(self, source_units: Mapping[str, str] | None = None) -> None:
        self._source_units = dict(source_units or {})

    def applies_to(self, expression: SpatialExpressionInput) -> bool:
        return any(part in expression.part_values for part in _METRIC_PARTS)

    def coordinate_inputs(self, expression: SpatialExpressionInput) -> Sequence[str]:
        """None: a unit conversion produces metrics, never a coordinate."""
        return ()

    def integrate(self, expression: SpatialExpressionInput) -> dict[str, float] | None:
        columns: dict[str, float] = {}
        for part, raw_value in expression.part_values.items():
            if part not in _METRIC_PARTS:
                continue
            source_unit = self._source_units.get(part, V.CANONICAL_UNIT[part])
            # unit_factor raises for an unknown (unit, column) pair — a typo in the
            # declared unit surfaces loudly instead of a silently wrong magnitude.
            columns[part] = _parse_float(part, raw_value) * V.unit_factor(source_unit, part)
        return columns or None


# --- the not-yet-available Methods (stubs only — no speculative machinery) ---

class _NotAvailableMethod:
    """Shared shape of the v1 stubs: ``applies_to`` recognises the referentials the
    future Method will serve; ``integrate`` refuses loudly. WHY a raising stub and
    not a silent None: a caller that reaches ``integrate`` believed the Method was
    available — returning nothing would present 'not implemented' as 'no result'."""

    name = ""
    version = "0"
    # The POSITION TYPES this mechanism will serve. Dispatching on the type rather than the
    # referential is the point of the split: a BSSID has no referential to match on.
    _position_types: frozenset[str] = frozenset()
    # The L1 part each mechanism will READ to produce a coordinate. Declared now so
    # ``position_raw`` records the right evidence the day the method is implemented —
    # an address for a geocoder, a cell or BSSID identifier for a location-service lookup —
    # rather than the angle parts a reprojection happens to use.
    _coordinate_input_parts: tuple[str, ...] = ("raw_position",)

    def applies_to(self, expression: SpatialExpressionInput) -> bool:
        return expression.position_type in self._position_types

    def coordinate_inputs(self, expression: SpatialExpressionInput) -> Sequence[str]:
        return [part for part in self._coordinate_input_parts
                if part in expression.part_values]

    def integrate(self, expression: SpatialExpressionInput) -> dict[str, float] | None:
        raise NotImplementedError(
            f"spatial method {self.name!r} is not available in v1: expression "
            f"{expression.spatial_expression_id} (referential "
            f"{expression.referential!r}) stays un-integrated")


class GeocodeAddress(_NotAvailableMethod):
    """Stub: geocode a postal-address expression to WGS84 (later task).

    The implemented method string must name its reference data
    (``geocode_address/<geocoder+register version>``).
    """

    name = "geocode_address"
    _position_types = frozenset({V.PositionType.STREET_ADDRESS.value})


class DecodeGridReference(_NotAvailableMethod):
    """Stub: decode a grid-cell code (geohash, MGRS, Plus Code) to WGS84 (later task).

    Self-contained arithmetic — no register, no gazetteer — which is why it sits beside the
    reprojection rather than with the lookups. The referential names WHICH grid.

    The implemented method must NAME the substitution it makes: a grid code denotes a CELL,
    so standing its centre in for the true position is ``grid_centre/<system>``, never a
    bare ``decode/...`` that hides an area behind a point.
    """

    name = "decode_grid_reference"
    _position_types = frozenset({V.PositionType.GRID_REFERENCE.value})


class CellRegisterLookup(_NotAvailableMethod):
    """Stub: resolve a cell reference through an antenna register (later task).

    The implemented method string must carry the register edition
    (``cell_register/<edition>``, e.g. ``cell_register/2025-03``) — the position is
    the ANTENNA's, dated by the register, not the device's.
    """

    name = "cell_register_lookup"
    _position_types = frozenset({V.PositionType.CELL_ID.value})


def _position_raw(expression: SpatialExpressionInput, parts: Sequence[str]) -> str | None:
    """The VERBATIM information that allowed ``latitude_wgs84``/``longitude_wgs84``.

    Rendered ``<source column>:<verbatim value>``, joined by `` | `` — e.g.
    ``ZLOCATIONLATITUDE:46.475 | ZLOCATIONLONGITUDE:6.418`` for a reprojection, and
    ``ZSTREET:12 Rue du Stand`` once a geocoder produces the same two columns from an
    address instead.

    ``parts`` comes from the PRODUCING method's ``coordinate_inputs``, so the scope is "what
    allowed the computation", not "the latitude and longitude components". The two coincide
    only while reprojection is the sole mechanism; a geocoder, a cell-register lookup or a
    BSSID location service each consume something else entirely and this records that
    instead, with no change here.

    WHY it lives on the INTEGRATION and not the expression: it records what THIS derivation
    consumed, so a reader can check the canonical coordinate against the exact source words
    behind it. An expression's other components — an altitude, an accuracy — had no part in
    producing the coordinate and are deliberately absent.

    A part with no source column renders its value alone, so nothing is silently dropped.
    """
    pairs = []
    for part in parts:
        value = expression.part_values.get(part)
        if value is None:
            continue
        column = expression.part_source_fields.get(part)
        pairs.append(f"{column}:{value}" if column else value)
    return " | ".join(pairs) or None


# --- the composer: one integration row per expression -------------------------

def integrate_expression(
    expression: SpatialExpressionInput,
    methods: Sequence[SpatialMethod],
) -> V.SpatialIntegration | None:
    """Compose the ONE ``spatial_integration`` row for an expression, or None.

    HOW: each applicable Method contributes its partial column mapping and its
    ``name/version`` fragment; contributions must be DISJOINT (two methods writing one
    column would be two derivations of one value — a mis-assembled method set, raised
    loudly); the fragments join with ``+`` in method-set order into the stored method
    string, and the row id derives from (expression id ⟂ method) via ``core.ids``.
    WHY None when nothing applied: an empty stamped row would claim a derivation that
    never ran; no row also means no S2 index row (the driver keys the index on the
    integration's coordinates).
    """
    columns: dict[str, float] = {}
    fragments: list[str] = []
    coordinate_inputs: list[str] = []
    for method in methods:
        if not method.applies_to(expression):
            continue
        produced = method.integrate(expression)
        if not produced:
            continue
        overlap = set(produced) & set(columns)
        if overlap:
            raise ValueError(
                f"methods overlap on columns {sorted(overlap)} for expression "
                f"{expression.spatial_expression_id}: one value, two derivations")
        # Only the method that actually produced a coordinate contributes the evidence
        # behind it: whatever mechanism it used, position_raw records ITS inputs.
        if _COORDINATE_COLUMNS & set(produced):
            coordinate_inputs.extend(method.coordinate_inputs(expression))
        columns.update(produced)
        fragments.append(f"{method.name}/{method.version}")
    if not columns:
        return None
    method_string = "+".join(fragments)
    return V.SpatialIntegration(
        # Empty unless a coordinate was produced: position_raw evidences THAT derivation,
        # so an expression yielding only metrics carries none.
        position_raw=_position_raw(expression, coordinate_inputs),
        spatial_integration_id=ids.new_id(),
        spatial_expression_id=expression.spatial_expression_id,
        method=method_string,
        referential=expression.referential,
        spatial_source=expression.spatial_source,
        **columns,
    )


# --- the store-level driver ---------------------------------------------------

@dataclass
class SpatialIntegrateReport:
    """The counts one ``integrate_spatial`` call produced (testable summary)."""

    integration_count: int = 0
    index_count: int = 0


def integrate_spatial(
    store: RunStore,
    *,
    source_units: Mapping[str, str] | None = None,
    methods: Sequence[SpatialMethod] | None = None,
) -> SpatialIntegrateReport:
    """Integrate every L1 spatial expression in ``store`` and index every point.

    HOW: load each ``spatial_expression`` + its components (ordinal order) through the
    store's read connection, compose the one integration row with the method set
    (default: ``PassthroughWgs84`` then ``UnitConvert(source_units)``), write it via the
    layer-typed L2 writer, and — whenever the row carries BOTH coordinates — write the
    one ``spatial_index`` leaf-cell row (level 30, signed storage form).
    ``source_units`` only parameterises the DEFAULT method set, so passing it beside
    an explicit ``methods`` would be silently ignored — that ambiguity raises instead.
    """
    if methods is not None and source_units is not None:
        raise ValueError(
            "pass source_units OR an explicit methods sequence, not both: "
            "source_units only feeds the default UnitConvert instance")
    if methods is None:
        methods = (PassthroughWgs84(), UnitConvert(source_units))

    report = SpatialIntegrateReport()
    for expression in _load_expressions(store.connection):
        integration = integrate_expression(expression, methods)
        if integration is None:
            continue
        store.write_spatial_integration(integration)
        report.integration_count += 1
        # one leaf cell per POINT — an integration without both coordinates
        # (metric-only, or an un-geocoded place) is not a point and gets no index row.
        if (integration.latitude_wgs84 is not None
                and integration.longitude_wgs84 is not None):
            store.write_spatial_index(V.SpatialIndex(
                spatial_index_id=ids.new_id(),
                spatial_integration_id=integration.spatial_integration_id,
                s2_cell_id=s2_leaf_cell_id(
                    integration.latitude_wgs84, integration.longitude_wgs84),
                s2_level=S2_LEAF_LEVEL,
            ))
            report.index_count += 1
    return report


def _load_expressions(connection: sqlite3.Connection) -> list[SpatialExpressionInput]:
    """Every NOT-yet-integrated L1 spatial expression with its part->raw_value mapping.

    HOW: one pass over ``spatial_component`` in (expression, ordinal) order builds the
    mappings; components with a NULL raw_value carry no value and are omitted. A part
    the v1 Methods interpret appearing TWICE in one expression raises — one canonical
    column cannot honestly hold two candidate raws (the single-row shape has no
    ambiguity rule; candidates arrive when the UNIQUE is dropped). A repeated
    NON-interpretable part (two raw_position fragments…) keeps its FIRST occurrence:
    nothing reads it in v1 and the verbatim rows all survive at L1 regardless.
    WHY the NOT-integrated filter: makes the pass idempotent — a re-run (or an append that
    adds new runs to an existing case) integrates only the new expressions and never collides
    on an existing ``spatial_integration`` row (same rule as the temporal pass).
    """
    expressions = {
        row[0]: SpatialExpressionInput(
            spatial_expression_id=row[0], referential=row[1], spatial_source=row[2],
            position_type=row[3])
        for row in connection.execute(
            "SELECT spatial_expression_id, referential, spatial_source, position_type "
            "FROM spatial_expression "
            "WHERE spatial_expression_id NOT IN "
            "(SELECT spatial_expression_id FROM spatial_integration) "
            "ORDER BY spatial_expression_id")
    }
    component_rows = connection.execute(
        "SELECT spatial_expression_id, part, raw_value, source_field FROM spatial_component "
        "ORDER BY spatial_expression_id, ordinal")
    for expression_id, part, raw_value, source_field in component_rows:
        if raw_value is None or expression_id not in expressions:
            continue   # NULL value, or a component of an already-integrated expression
        if source_field is not None:
            expressions[expression_id].part_source_fields.setdefault(part, source_field)
        part_values = expressions[expression_id].part_values
        if part in part_values:
            if part in _INTERPRETABLE_PARTS:
                raise ValueError(
                    f"spatial expression {expression_id} carries part {part!r} twice: "
                    f"one canonical column cannot hold two candidate values")
            continue   # non-interpretable repeat: first occurrence wins, raws stay at L1
        part_values[part] = raw_value
    return list(expressions.values())
