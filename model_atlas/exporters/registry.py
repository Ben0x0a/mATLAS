"""Additional-export plugin registry and runner.

Defines:    ExportContext (everything an exporter receives), ExporterSpec, the
            register_exporter decorator, available_exporters, run_exporters, and
            load_plugins (drop-in .py discovery).
Used by:    exporters.builtin (registers the shipped exporters), pipeline (runs the selected
            exporters after the primary CSV), launcher.cli + gui.app (list/select them).
Depends on: pandas + standard library only. Mirrors transforms.registry's decorator pattern.

An exporter is ``fn(ctx: ExportContext) -> list[Path]`` returning the files it wrote. Registering
under a duplicate name is a hard error (like register_transform) so a clashing plugin is caught,
not silently shadowing a builtin.
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportContext:
    """Everything an exporter could need for one output artefact.

    In merge mode there is one context (``preset_name`` is None); in split mode one per preset.
    ``dataframe`` is the final, untangled, column-ordered frame that was just written as CSV."""

    dataframe: pd.DataFrame
    output_dir: Path                 # directory to write sibling files into
    base_name: str                   # stem to base sibling filenames on (e.g. "merged" or preset)
    merge: bool
    preset_name: str | None
    primary_csv: Path | None         # the CSV already written (reference)
    row_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)      # per-source provenance records
    started_at: str | None = None
    input_path: Path | None = None
    presets_path: Path | None = None
    settings: Any = None             # model_atlas.config.Settings (untyped to avoid an import cycle)


Exporter = Callable[[ExportContext], "list[Path]"]


@dataclass(frozen=True)
class ExporterSpec:
    name: str            # CLI id, e.g. "sqlite"
    label: str           # GUI label, e.g. "SQLite database"
    description: str
    func: Exporter


_REGISTRY: dict[str, ExporterSpec] = {}


def register_exporter(name: str, label: str, description: str = "") -> Callable[[Exporter], Exporter]:
    """Register an exporter under ``name``. A duplicate name is a hard error so a clashing plugin
    is surfaced rather than silently overriding a builtin."""
    def decorator(func: Exporter) -> Exporter:
        if name in _REGISTRY:
            raise ValueError(f"Exporter {name!r} is already registered")
        _REGISTRY[name] = ExporterSpec(name=name, label=label, description=description, func=func)
        return func

    return decorator


def available_exporters() -> list[ExporterSpec]:
    """All registered exporters, sorted by label (the order shown in the CLI help and GUI menu)."""
    return sorted(_REGISTRY.values(), key=lambda spec: spec.label.casefold())


def get_exporter(name: str) -> ExporterSpec | None:
    return _REGISTRY.get(name)


def run_exporters(names: "list[str] | None", ctx: ExportContext) -> list[Path]:
    """Run each selected exporter in turn, returning every file written. An unknown name is a
    warning; an exporter that raises is logged at ERROR and skipped so one bad exporter never
    aborts the run or the others."""
    written: list[Path] = []
    for name in names or []:
        spec = _REGISTRY.get(name)
        if spec is None:
            log.warning(f"Unknown export {name!r}; skipping (available: {[s.name for s in available_exporters()]})")
            continue
        try:
            paths = spec.func(ctx) or []
        except Exception as exc:  # noqa: BLE001 - one exporter must not break the run or the rest.
            log.error(f"Export {name!r} failed: {type(exc).__name__}: {exc}")
            continue
        written.extend(paths)
        log.info(f"Export {name!r} wrote {len(paths)} file(s)")
    return written


_loaded_dirs: set[str] = set()


def load_plugins(plugins_dir: Path) -> None:
    """Import every ``*.py`` (not ``_``-prefixed) in ``plugins_dir`` so its @register_exporter runs.
    A missing folder is a no-op; a broken plugin is logged and skipped, never fatal. Idempotent per
    directory so repeated calls (CLI + GUI in one process) do not re-import."""
    directory = Path(plugins_dir)
    key = str(directory.resolve()) if directory.exists() else str(directory)
    if key in _loaded_dirs:
        return
    _loaded_dirs.add(key)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"matlas_export_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            log.info(f"Loaded export plugin {path.name}")
        except Exception as exc:  # noqa: BLE001 - a bad plugin must not stop the app from starting.
            log.error(f"Failed to load export plugin {path.name}: {type(exc).__name__}: {exc}")
