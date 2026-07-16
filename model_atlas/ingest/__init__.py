"""Ingest bricks: format readers behind the ``Reader`` protocol (docs/refactor_plan.md §3).

Defines:    nothing itself — a namespace package for ``ingest.readers`` (T20) and, later,
            ``discover``/``containers``/``fingerprint``/``presets``/``assemble`` (T21-T23).
Used by:    the app-layer pipeline (once it exists) and tests/ingest/.
Depends on: model_atlas.core only, per the dependency-arrow-points-inward rule.
"""
from __future__ import annotations
