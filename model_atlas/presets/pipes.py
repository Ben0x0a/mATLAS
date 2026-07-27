"""The preset pipe language: named transform registry, pipe runner and built-in pipes.

Defines:    register_transform (decorator), TransformHardError, PipeContext, run_pipe —
            which executes a parsed pipe (a tuple of expr.PipeCall) left to right,
            applying each step's on_error policy — and the built-in pipe transforms
            (cast, scale, arithmetic, lookup, regex, split).
Used by:    presets.resolve (runs pipes inside field resolution) and, through it, the
            ingest assembly engine.
Depends on: presets.expr (PipeCall), presets.expression (the arithmetic sandbox),
            presets.value_maps (lookup key normalisation); standard library.

The registry/runner half is moved verbatim from
``transforms/registry.py`` and the six pipe transforms from
``transforms/builtin.py``. WHY one module: the pipes are the execution
half of the preset pipe LANGUAGE (``presets.expr`` parses ``PipeCall``s; this runs
them), so they live in the presets package beside their parser — the zone/ISO
timestamp primitives that shared the old builtin module are integration concerns and
moved to ``model_atlas/integrate/timeparse.py`` instead.

A transform is ``fn(value, args, kwargs, ctx) -> value`` and treats ``None`` as a
no-op so an empty source cell flows through a pipe untouched rather than raising.
``ctx`` (PipeContext) carries the preset's named lookup tables and regex patterns so
``lookup(name)`` / ``regex(name, group=...)`` resolve by name.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from model_atlas.presets.expr import PipeCall
from model_atlas.presets.expression import evaluate
from model_atlas.presets.value_maps import value_map_key

log = logging.getLogger(__name__)

_ON_ERROR_POLICIES = ("null", "raw", "error")

_TRUE_TOKENS = {"1", "true", "yes", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "n", "f", ""}

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


# --- the built-in pipe transforms (value, args, kwargs, ctx) ----------------

@register_transform("cast")
def _cast(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    if value is None:
        return None
    to = args[0] if args else kwargs.get("to")
    if to == "int":
        return int(value)
    if to == "float":
        return float(value)
    if to == "str":
        return str(value)
    if to == "bool":
        token = str(value).strip().casefold()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ValueError(f"Cannot cast {value!r} to bool")
    raise TransformHardError(f"Unsupported cast target {to!r}")


@register_transform("scale")
def _scale(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    if value is None:
        return None
    factor = args[0] if args else kwargs.get("by")
    return float(value) * float(factor)


@register_transform("arithmetic")
def _arithmetic(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    if value is None:
        return None
    return evaluate(str(args[0]), value)


@register_transform("lookup")
def _lookup(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    if value is None:
        return None
    name = args[0] if args else None
    table = ctx.lookup_tables.get(str(name))
    if table is None:
        raise TransformHardError(f"lookup references unknown table {name!r}")
    on_unknown = kwargs.get("on_unknown", "raw")
    normalised = {value_map_key(k): v for k, v in table.items()}
    key = value_map_key(value)
    if key in normalised:
        return normalised[key]
    if on_unknown == "raw":
        return value
    if on_unknown is None or on_unknown == "null":
        return None
    if on_unknown == "error":
        raise TransformHardError(f"No mapping for {value!r} in {name!r} and on_unknown=error")
    raise TransformHardError(f"on_unknown must be raw|null|error, got {on_unknown!r}")


@register_transform("regex")
def _regex(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    """Return a NAMED capture group of a named pattern (named groups are mandatory)."""
    if value is None:
        return None
    name = str(args[0]) if args else None
    pattern = ctx.patterns.get(name)
    if pattern is None:
        raise TransformHardError(f"regex references unknown pattern {name!r}")
    group = kwargs.get("group")
    if group is None:
        raise TransformHardError(f"regex({name}) requires group=<named group>")
    compiled = re.compile(pattern)
    if str(group) not in compiled.groupindex:
        raise TransformHardError(f"pattern {name!r} has no named group {group!r}")
    match = compiled.search(str(value))
    return match.group(str(group)) if match else None


@register_transform("split")
def _split(value: Any, args: tuple[Any, ...], kwargs: dict[str, Any], ctx: PipeContext) -> Any:
    if value is None:
        return None
    separator = str(args[0])
    parts = str(value).split(separator)
    index = kwargs.get("index")
    if index is None:
        return parts
    return parts[int(index)]
