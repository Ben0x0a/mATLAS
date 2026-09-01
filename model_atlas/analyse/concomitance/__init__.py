"""Concomitance: which placements of one group are near which placements of another.

Defines:    the public surface — ``find_concomitant`` and ``RunSummary`` /
            ``SpatialStrategy`` / ``EARTH_RADIUS_M`` (engine), the request layer
            (``ConcomitanceRequest``, ``Criterion``, ``EntityGroup``, ``MatchMode``,
            ``Severity``, ``Problem``, ``check_request``, ``duration_from``,
            ``DURATION_UNITS``, ``DURATION_UNIT_NAMES``) and the output layer
            (``MatchRow``, ``MatchSink``, ``CsvMatchSink``, ``CSV_HEADER``).
Used by:    ``model_atlas.app.concomitance_run`` (the only caller either shell uses),
            ``launcher.cli`` and ``gui`` for the request/output shapes.
Depends on: ``model_atlas.analyse.helpers``, ``model_atlas.core``,
            ``model_atlas.integrate.spatial``.

Callers import from THIS package, not from its modules, so the internal split between
engine / request / output can change without touching a caller.
"""
from model_atlas.analyse.concomitance.engine import (
    EARTH_RADIUS_M,
    RunSummary,
    SpatialStrategy,
    find_concomitant,
    great_circle_distance_m,
)
from model_atlas.analyse.concomitance.output import (
    CSV_HEADER,
    CsvMatchSink,
    MatchRow,
    MatchSink,
)
from model_atlas.analyse.concomitance.request import (
    DURATION_UNIT_NAMES,
    DURATION_UNITS,
    ConcomitanceRequest,
    Criterion,
    EntityGroup,
    MatchMode,
    Problem,
    Severity,
    check_request,
    duration_from,
    same_selection,
)

__all__ = [
    "EARTH_RADIUS_M", "RunSummary", "SpatialStrategy", "find_concomitant",
    "great_circle_distance_m",
    "CSV_HEADER", "CsvMatchSink", "MatchRow", "MatchSink",
    "DURATION_UNITS", "DURATION_UNIT_NAMES", "ConcomitanceRequest", "Criterion",
    "EntityGroup", "MatchMode", "Problem", "Severity", "check_request",
    "duration_from", "same_selection",
]
