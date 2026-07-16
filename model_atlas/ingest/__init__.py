"""Ingest bricks: format readers behind the ``Reader`` protocol (docs/refactor_plan.md §3).

Defines:    nothing itself — a namespace package for ``ingest.readers`` (T20), the L0
            walk/writer (``containers``/``format_detect``/``pathmatch``/``discover``/
            ``writer`` — T21) and, later, ``presets``/``assemble`` (T23).
Used by:    the app-layer pipeline (once it exists) and tests/ingest/.
Depends on: mainly ``model_atlas.core``; the readers and the L0 writer also reuse the
            stable ``model_atlas.sqlite`` / ``model_atlas.integrity`` leaf helpers (the
            strategic zip fingerprint, whole-file sha256) rather than re-porting them.
"""
from __future__ import annotations
