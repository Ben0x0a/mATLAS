"""The ``format -> Reader`` registry: add a format by adding a module + one register call.

Defines:    ``register_reader`` (class decorator), ``get_reader``, ``registered_readers``.
Used by:    every concrete reader module (decorates its class) and, later, the assemble
            brick (T23) to dispatch a source's declared format to its reader.
Depends on: ``model_atlas.ingest.readers.base`` (the ``Reader`` protocol) only.

WHY a class decorator (mirrors the old ``model_atlas/sources/readers/base.py`` pattern):
one line at the reader's definition site registers it — no separate wiring file to keep in
sync, so a new format module is genuinely the only change (docs/refactor_plan.md §3,
contract 4: "no core edit").
"""
from __future__ import annotations

import logging

from model_atlas.ingest.readers.base import Reader

log = logging.getLogger(__name__)

_READERS: dict[str, Reader] = {}


def register_reader(cls: type) -> type:
    instance = cls()
    _READERS.setdefault(instance.format, instance)
    log.debug(f"Registered ingest reader: {instance.format}")
    return cls


def get_reader(fmt: str) -> Reader:
    reader = _READERS.get(fmt)
    if reader is None:
        raise ValueError(f"no ingest reader for format {fmt!r}")
    return reader


def registered_readers() -> tuple[Reader, ...]:
    return tuple(_READERS.values())
