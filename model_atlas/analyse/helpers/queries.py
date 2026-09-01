"""Query fragments every analysis shares — the one place they are declared.

Defines:    ``FROM_JOINS`` (the observation -> entity/spatial/temporal join spine used by
            any analysis that needs a placed, timed observation) and the re-exported
            ``spatial_part`` / ``temporal_part`` component subqueries.
Used by:    ``model_atlas.analyse.concomitance.engine``,
            ``model_atlas.analyse.helpers.case_vocabulary``, and every future analysis.
Depends on: ``model_atlas.core.flat_view`` only.

WHY this module exists: an analysis must score EXACTLY the value the flat CSV shows. Each
analysis re-deriving its own join or its own component subquery is how the two silently
diverge — one picking a different component row than the projection does. Declaring the
spine once, here, makes that divergence impossible, and gives the analyses a shared
dependency to import instead of importing one another.
"""
from __future__ import annotations

# The component subqueries the flat projection itself uses. Re-exported (not re-written)
# so an analysis provably reads the projection's value: a locally re-derived
# ``(SELECT ... LIMIT 1)`` could order rows differently and pick another component.
from model_atlas.core.flat_view import _spatial_part as spatial_part
from model_atlas.core.flat_view import _temporal_part as temporal_part

# The join spine from an observation to its entity and its integrated corners. An analysis
# that needs a placed, timed observation starts here, so anchor and partner populations are
# drawn identically — an asymmetry between two hand-written joins silently loses rows.
# The spatial_index join is part of the definition: spatial integration writes one index row
# per integrated point, so a point without one is not a queryable point.
FROM_JOINS = (
    " FROM observation a"
    " JOIN entity e ON e.entity_id = a.entity_id"
    " JOIN spatial_integration si"
    "   ON si.spatial_expression_id = a.spatial_expression_id"
    " JOIN spatial_index six"
    "   ON six.spatial_integration_id = si.spatial_integration_id"
    " JOIN timespan_integration ti"
    "   ON ti.temporal_expression_id = a.temporal_expression_id"
)

__all__ = ["FROM_JOINS", "spatial_part", "temporal_part"]
