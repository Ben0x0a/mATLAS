"""Named transform registry and pipe runner (v3).

Defines:    register_transform (decorator), TransformHardError, PipeContext, and
            run_pipe — which executes a parsed pipe (a tuple of expr.PipeCall) left
            to right, applying each step's on_error policy.
Used by:    transforms.builtin (registers transforms), transforms.assemble (runs pipes).
Depends on: presets.expr (PipeCall); standard library.

A transform is ``fn(value, args, kwargs, ctx) -> value`` and treats ``None`` as a
no-op. ``ctx`` (PipeContext) carries the preset's named lookup tables and regex
patterns so ``lookup(name)`` / ``regex(name, group=...)`` resolve by name.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from model_atlas.presets.expr import PipeCall

log = logging.getLogger(__name__)

_ON_ERROR_POLICIES = ("null", "raw", "error")

Transform = Callable[[Any, tuple[Any, ...], dict[str, Any], "PipeContext"], Any]
_REGISTRY: dict[str, Transform] = {}


class TransformHardError(ValueError):
    """A deliberate, config-driven halt (unknown transform, on_unknown=error, etc.)
    that the pipe always propagates regardless of a step's on_error policy."""


@dataclass(frozen=True)
class PipeContext:
    """Per-preset data a pipe step may reference by name."""

    lookup_tables: dict[str, dict[Any, Any]] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)


def register_transform(name: str) -> Callable[[Transform], Transform]:
    def decorator(func: Transform) -> Transform:
        if name in _REGISTRY:
            raise ValueError(f"Transform {name!r} is already registered")
        _REGISTRY[name] = func
        return func

    return decorator


def run_pipe(
    value: Any, calls: tuple[PipeCall, ...], ctx: PipeContext, warnings: list[str]
) -> Any:
    """Run a parsed pipe left to right, returning the final value."""
    for call in calls:
        func = _REGISTRY.get(call.name)
        if func is None:
            raise TransformHardError(f"Unknown transform {call.name!r}")
        kwargs = dict(call.kwargs)
        on_error = kwargs.pop("on_error", "null")
        if on_error not in _ON_ERROR_POLICIES:
            raise TransformHardError(f"on_error must be one of {_ON_ERROR_POLICIES}, got {on_error!r}")
        try:
            value = func(value, call.args, kwargs, ctx)
        except TransformHardError:
            raise
        except Exception as exc:  # noqa: BLE001 - the policy decides how to surface it.
            if on_error == "error":
                raise
            message = f"transform {call.name!r} failed on {value!r}: {exc} (on_error={on_error})"
            log.warning(message)
            warnings.append(message)
            value = value if on_error == "raw" else None
    return value
