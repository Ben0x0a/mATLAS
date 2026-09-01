"""Untangle: rank the records of one event so the best reading is identifiable.

Defines:    ``analyse`` (the entry point the pipeline calls), ``METHOD_NAME`` and
            ``UNTANGLE_VERSION``.
Used by:    ``model_atlas.app.pipeline``.
Depends on: ``model_atlas.analyse.helpers``, ``model_atlas.core``.
"""
from model_atlas.analyse.untangle.ranker import METHOD_NAME, UNTANGLE_VERSION, analyse

__all__ = ["analyse", "METHOD_NAME", "UNTANGLE_VERSION"]
