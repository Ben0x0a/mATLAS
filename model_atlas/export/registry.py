"""New-engine export-brick registry (T51): register / list / run the query+serialiser
``Exporter`` bricks in this package.

Defines:    ExporterSpec, register_exporter, available_exporters, get_exporter,
            run_exporters — the same register/list/run shape as
            ``model_atlas.exporters.registry``, mirrored for the new ``Exporter``
            protocol (``export(store, ctx) -> list[Path]``, T50).
Used by:    kml.py / geojson.py / xlsx.py / geovisualizer.py / sqlite_db.py (each
            registers its brick instance at import time) and tests/export/*.
Depends on: model_atlas.core.store.RunStore, model_atlas.export.csv_flat (the
            ``Exporter`` protocol + ``ExportContext``), the standard library only.

Why a second registry, rather than reusing model_atlas/exporters/registry.py (T51 fence)
------------------------------------------------------------------------------------------
``model_atlas/exporters/registry.py`` is FENCED for this task and its
``ExportContext`` carries a pre-built pandas ``DataFrame`` plus a raw
``sqlite3.Connection`` — the OLD exporter contract, ``fn(ctx) -> list[Path]``. The
bricks in this package follow the T50 ``Exporter`` protocol instead —
``export(store: RunStore, ctx: ExportContext) -> list[Path]`` — so they query the
store directly rather than receiving an already-assembled frame. The two contracts
cannot share one registry without changing the fenced module, so this is a
deliberately small, parallel mirror of the same mechanism, scoped to the new engine.
A later switch task rewires ``model_atlas.pipeline`` to run exporters from THIS
registry (dropping the old one) once the app-layer pipeline is cut over (T60); until
then both registries coexist and neither is driven by the app yet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import Exporter, ExportContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExporterSpec:
    name: str            # brick id, e.g. "kml" (matches Exporter.format)
    label: str            # human label, e.g. "KML"
    description: str
    exporter: Exporter


_REGISTRY: dict[str, ExporterSpec] = {}


def register_exporter(name: str, label: str, exporter: Exporter, description: str = "") -> None:
    """Register one ``Exporter`` INSTANCE under ``name``.

    A duplicate name is a hard error (mirrors ``exporters.registry.register_exporter``)
    so a clashing brick is caught at import time, never silently shadowing another.
    """
    if name in _REGISTRY:
        raise ValueError(f"Exporter {name!r} is already registered")
    _REGISTRY[name] = ExporterSpec(name=name, label=label, description=description, exporter=exporter)


def available_exporters() -> list[ExporterSpec]:
    """All registered bricks, sorted by label (mirrors the old registry's ordering)."""
    return sorted(_REGISTRY.values(), key=lambda spec: spec.label.casefold())


def get_exporter(name: str) -> ExporterSpec | None:
    return _REGISTRY.get(name)


def run_exporters(names: "list[str] | None", store: RunStore, ctx: ExportContext) -> list[Path]:
    """Run each selected brick in turn, returning every file written.

    An unknown name is a warning; a brick that raises is logged at ERROR and skipped,
    so one bad exporter never aborts the run or the others — the same resilience rule
    as ``exporters.registry.run_exporters``.
    """
    written: list[Path] = []
    for name in names or []:
        spec = _REGISTRY.get(name)
        if spec is None:
            log.warning(f"Unknown export {name!r}; skipping (available: {[s.name for s in available_exporters()]})")
            continue
        try:
            paths = spec.exporter.export(store, ctx) or []
        except Exception as exc:  # noqa: BLE001 - one exporter must not break the run or the rest.
            log.error(f"Export {name!r} failed: {type(exc).__name__}: {exc}")
            continue
        written.extend(paths)
        log.info(f"Export {name!r} wrote {len(paths)} file(s)")
    return written
