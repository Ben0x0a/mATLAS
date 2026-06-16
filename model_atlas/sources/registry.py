"""Source-adapter registry.

Defines:    register_adapter (class decorator), get_adapter (dispatch by element),
            registered_adapters (inspection).
Used by:    the csv/excel/sqlite adapter modules (to register) and the pipeline
            (to dispatch extraction without a hardcoded if/elif).
Depends on: models (DiscoveredElement), sources.base (SourceAdapter).
"""
from __future__ import annotations

import logging

from model_atlas.models import DiscoveredElement
from model_atlas.sources.base import SourceAdapter

log = logging.getLogger(__name__)

_ADAPTERS: list[SourceAdapter] = []


def register_adapter(cls: type) -> type:
    """Instantiate and register an adapter class. Idempotent by ``name`` so a
    re-imported module does not register a duplicate."""
    instance = cls()
    if any(existing.name == instance.name for existing in _ADAPTERS):
        return cls
    _ADAPTERS.append(instance)
    log.debug("Registered source adapter: %s", instance.name)
    return cls


def get_adapter(element: DiscoveredElement) -> SourceAdapter:
    """Return the first registered adapter that can handle ``element``."""
    for adapter in _ADAPTERS:
        if adapter.can_handle(element):
            return adapter
    # No adapter is a hard stop: the element cannot be extracted, so processing it
    # would silently drop evidence.
    raise ValueError(f"No source adapter for source type {element.source_type.value!r}")


def registered_adapters() -> tuple[SourceAdapter, ...]:
    return tuple(_ADAPTERS)
