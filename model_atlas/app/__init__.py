"""The application layer: orchestration and run-level support for the relational engine.

Defines:    nothing itself — a namespace for ``app.pipeline`` (the ``process()``
            orchestrator over the ingest/integrate/analyze/export bricks),
            ``app.config`` (operator settings), ``app.reporting`` (sidecar reports),
            ``app.diagnostics`` (the crash-report excepthook) and
            ``app.logging_setup`` (log formatting/configuration).
Used by:    ``launcher.cli``, ``launcher.gui`` and ``gui.app`` (the entry points), and
            ``model_atlas.__init__`` (re-exports ``process`` / ``ProcessResult``).
Depends on: the engine bricks (``core``, ``ingest``, ``integrate``, ``analyze``,
            ``export``, ``presets``) — the app layer only wires them together.
"""
from __future__ import annotations
