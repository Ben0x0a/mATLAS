"""New-engine export-brick registry (T51): register / list / run the query+serialiser
``Exporter`` bricks in this package.

Defines:    ExporterSpec, register_exporter, available_exporters, get_exporter,
            run_exporters (register / list / run for the ``Exporter`` protocol —
            ``export(store, ctx) -> list[Path]``, T50), ``load_builtin_exporters``
            (imports every shipped brick for its registration side effect) and the
            registration of the primary ``csv`` brick.
Used by:    the app-layer pipeline (``model_atlas.app.pipeline`` runs the selected
            bricks), gui.app / launcher.cli (list the available bricks),
            kml.py / geojson.py / xlsx.py / geovisualizer.py / sqlite_db.py (each
            registers its brick instance at import time) and tests/export/*.
Depends on: model_atlas.core.store.RunStore, model_atlas.export.csv_flat (the
            ``Exporter`` protocol + ``ExportContext``), the standard library only.

T60: this became the ONE exporter registry — the old flat-frame registry (its
``ExportContext`` carried a pre-built pandas ``DataFrame``; ``fn(ctx)`` contract)
retired with the old engine, and the app pipeline now drives these bricks directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import CsvFlatExporter, Exporter, ExportContext

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


def load_builtin_exporters() -> None:
    """Import every shipped export-brick module for its registration side effect.

    HOW: plain lazy imports inside the function — the bricks import THIS module to
    register themselves, so a module-level import here would be a cycle. WHY needed:
    the GUI/CLI list the available bricks before any run; without this call only the
    ``csv`` brick (registered below) would be visible."""
    import model_atlas.export.geojson  # noqa: F401 - registration side effect
    import model_atlas.export.geovisualizer  # noqa: F401
    import model_atlas.export.kml  # noqa: F401
    import model_atlas.export.sqlite_db  # noqa: F401
    import model_atlas.export.xlsx  # noqa: F401


# The primary flat-CSV brick, registered HERE rather than in csv_flat.py: the registry
# already imports csv_flat (the Exporter protocol lives there), so a csv_flat-side
# registration would be a circular import. Registered under the historic name "csv" so
# the GUI's default selection and the CLI's --export vocabulary carry over unchanged.
register_exporter(
    "csv", "CSV", CsvFlatExporter(),
    "The flat assertion CSV (merged, or one file per preset in split mode).",
)
