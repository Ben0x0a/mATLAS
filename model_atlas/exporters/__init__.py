"""Additional-export subsystem: a small plugin registry for extra output formats.

Importing this package registers the built-in exporters (sqlite / geojson / xlsx / kml /
geovisualizer) as a side effect, and exposes the drop-in contract for custom exporters.

Public API:
    register_exporter, ExportContext, ExporterSpec, available_exporters, get_exporter,
    run_exporters, load_plugins.

A custom exporter is a .py file dropped in the configured plugins folder (see
``[export].plugins_dir`` in matlas_config.toml)::

    from model_atlas.exporters import register_exporter, ExportContext

    @register_exporter("my_csv", "My custom CSV", "Semicolon-delimited variant")
    def export(ctx: ExportContext) -> list[Path]:
        out = ctx.output_dir / f"{ctx.base_name}.custom.csv"
        ctx.dataframe.to_csv(out, sep=";", index=False)
        return [out]
"""
from __future__ import annotations

from model_atlas.exporters.registry import (
    ExportContext,
    ExporterSpec,
    available_exporters,
    get_exporter,
    load_plugins,
    register_exporter,
    run_exporters,
)

# Import for side effects: the shipped exporters self-register on import.
from model_atlas.exporters import builtin as _builtin  # noqa: F401,E402

__all__ = [
    "ExportContext",
    "ExporterSpec",
    "available_exporters",
    "get_exporter",
    "load_plugins",
    "register_exporter",
    "run_exporters",
]
