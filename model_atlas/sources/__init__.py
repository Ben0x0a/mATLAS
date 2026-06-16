"""Source discovery and pluggable extraction adapters.

Importing this package registers the built-in adapters (csv, excel, sqlite) so the
pipeline can dispatch extraction through the registry rather than a hardcoded
if/elif.
"""
from __future__ import annotations

from model_atlas.sources.folder import discover_elements
from model_atlas.sources.registry import get_adapter, registered_adapters

# Imported for their side effect: each module registers its adapter on import.
from model_atlas.sources import (  # noqa: E402,F401
    csv_source,
    excel_source,
    sqlite_source,
)

__all__ = ["discover_elements", "get_adapter", "registered_adapters"]
