"""Code shared by the analyses — the ONLY thing one analysis may import from another's side.

Defines:    re-exports of ``FROM_JOINS`` / ``spatial_part`` / ``temporal_part`` (queries)
            and ``CriterionKind`` / ``VocabularyEntry`` / ``read_case_vocabulary``
            (case_vocabulary).
Used by:    every package under ``model_atlas.analyse``.
Depends on: ``model_atlas.core`` only.

Nothing here carries a verdict. A module that decides something about the evidence is an
analysis and belongs in its own package; this one only supplies the shared query spine and
the "which tokens exist in this case" lookup that several analyses need to offer choices.
"""
from model_atlas.analyse.helpers.case_vocabulary import (
    CriterionKind,
    VocabularyEntry,
    read_case_vocabulary,
)
from model_atlas.analyse.helpers.queries import FROM_JOINS, spatial_part, temporal_part

__all__ = [
    "FROM_JOINS", "spatial_part", "temporal_part",
    "CriterionKind", "VocabularyEntry", "read_case_vocabulary",
]
