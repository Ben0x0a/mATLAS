"""Named transform registry and per-field pipe runner.

Defines:    register_transform (decorator), get_transform, apply_step, apply_pipe.
            A preset field declares a `pipe:` list of single-transform steps; this
            module resolves each step to a registered function and runs them
            left-to-right, applying the per-step on_error policy.
Used by:    transforms.builtin (registers the builtins), and the assembly engine
            (applies a field's pipe).
Depends on: standard library only.

Step shape (from YAML): a mapping whose one key matching a registered transform
name carries that transform's primary argument; any remaining keys are extra
parameters, including the optional ``on_error``. Examples:
    {arithmetic: "(value + 1) * 1000"}
    {cast: int}
    {lookup: {1: GNSS, 4: WiFi}, on_unknown: raw}
    {regex_extract: "rowid=(\\d+)", group: 1, on_error: error}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

_ON_ERROR_POLICIES = ("null", "raw", "error")


class TransformHardError(ValueError):
    """A deliberate, policy-driven halt (e.g. ``on_unknown: error``, a misconfigured
    transform) that the pipe must always propagate, regardless of a step's ``on_error``.
    Distinguishes author/config faults from per-row data failures, which ``on_error`` governs.
    """


@dataclass(frozen=True)
class _Transform:
    name: str
    func: Callable[..., Any]
    primary: str  # the parameter name the single-key step value binds to


_REGISTRY: dict[str, _Transform] = {}


def register_transform(name: str, *, primary: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a transform under ``name`` whose single-key value binds to ``primary``."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        # A duplicate name is a programming error, not a runtime variant — fail loudly
        # so two transforms cannot silently shadow each other.
        if name in _REGISTRY:
            raise ValueError(f"Transform {name!r} is already registered")
        _REGISTRY[name] = _Transform(name=name, func=func, primary=primary)
        return func

    return decorator


def get_transform(name: str) -> _Transform:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown transform {name!r}")
    return _REGISTRY[name]


def _resolve_step(step: dict[str, Any]) -> tuple[_Transform, Any, dict[str, Any]]:
    if not isinstance(step, dict):
        raise ValueError(f"Pipe step must be a mapping, got {type(step).__name__}")
    transform_keys = [key for key in step if key in _REGISTRY]
    # Exactly one key must name a transform; zero or several is an authoring error.
    if len(transform_keys) != 1:
        raise ValueError(
            f"Pipe step must name exactly one transform; found {transform_keys or 'none'} in {step}"
        )
    name = transform_keys[0]
    transform = _REGISTRY[name]
    extra = {key: value for key, value in step.items() if key != name}
    return transform, step[name], extra


def apply_step(value: Any, step: dict[str, Any], warnings: list[str]) -> Any:
    """Run one pipe step against ``value``; record a warning per the on_error policy."""
    transform, primary_value, extra = _resolve_step(step)
    on_error = extra.pop("on_error", "null")
    if on_error not in _ON_ERROR_POLICIES:
        raise ValueError(f"on_error must be one of {_ON_ERROR_POLICIES}, got {on_error!r}")
    params = {transform.primary: primary_value, **extra}
    try:
        return transform.func(value, **params)
    except TransformHardError:
        # A deliberate/config halt is never swallowed by the per-row on_error policy.
        raise
    except Exception as exc:  # noqa: BLE001 - the policy decides how to surface it.
        if on_error == "error":
            raise
        message = f"transform {transform.name!r} failed on {value!r}: {exc} (on_error={on_error})"
        log.warning(message)
        warnings.append(message)
        # "raw" keeps the input value; "null" drops it.
        return value if on_error == "raw" else None


def apply_pipe(value: Any, steps: list[dict[str, Any]] | None) -> tuple[Any, list[str]]:
    """Run a field's pipe left-to-right, returning the final value and any warnings."""
    warnings: list[str] = []
    for step in steps or []:
        value = apply_step(value, step, warnings)
    return value, warnings
