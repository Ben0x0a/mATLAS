"""Built-in transforms for the per-field pipe.

Defines:    arithmetic, cast, lookup, regex_extract, split — each registered into
            the transform registry on import.
Used by:    the assembly engine via transforms.registry; importing this module is
            what makes the builtins available.
Depends on: transforms.expression (sandbox), transforms.value_maps (key normaliser),
            transforms.registry.

Every transform treats ``None`` as a no-op (returns ``None``) so an empty source
cell flows through a pipe untouched rather than raising.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from model_atlas.transforms.expression import evaluate
from model_atlas.transforms.registry import TransformHardError, register_transform
from model_atlas.transforms.value_maps import value_map_key

_TRUE_TOKENS = {"1", "true", "yes", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "n", "f", ""}


@register_transform("arithmetic", primary="expression")
def arithmetic(value: Any, *, expression: str) -> Any:
    """Evaluate a sandboxed arithmetic expression with ``value`` bound."""
    if value is None:
        return None
    return evaluate(expression, value)


@register_transform("cast", primary="to")
def cast(value: Any, *, to: str) -> Any:
    """Coerce ``value`` to ``int`` | ``float`` | ``str`` | ``bool``."""
    if value is None:
        return None
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
        # An ambiguous token is a data fault — surface it to the on_error policy.
        raise ValueError(f"Cannot cast {value!r} to bool")
    # An unknown cast target is an authoring fault — always halt.
    raise TransformHardError(f"Unsupported cast target {to!r}")


@register_transform("lookup", primary="table")
def lookup(value: Any, *, table: dict[Any, Any], on_unknown: str = "raw") -> Any:
    """Map an encoded ``value`` to a label via ``table`` (e.g. {1: GNSS, 4: WiFi}).

    YAML numeric and string keys are treated equivalently. ``on_unknown`` decides
    an unmapped value: ``raw`` keeps it, ``null`` drops it, ``error`` raises.
    """
    if value is None:
        return None
    normalised = {value_map_key(key): label for key, label in table.items()}
    key = value_map_key(value)
    if key in normalised:
        return normalised[key]
    if on_unknown == "raw":
        return value
    if on_unknown == "null":
        return None
    if on_unknown == "error":
        # Author asked to halt on an unmapped code — a deliberate hard stop.
        raise TransformHardError(f"No mapping for {value!r} and on_unknown=error")
    raise TransformHardError(f"on_unknown must be raw|null|error, got {on_unknown!r}")


@register_transform("regex_extract", primary="pattern")
def regex_extract(value: Any, *, pattern: str, group: int | str = 1) -> Any:
    """Return the capture ``group`` of the first match of ``pattern``, else None."""
    if value is None:
        return None
    match = re.search(pattern, str(value))
    if match is None:
        return None
    return match.group(group)


@register_transform("split", primary="separator")
def split(value: Any, *, separator: str, index: int | None = None) -> Any:
    """Split on ``separator``; return part ``index`` if given, else the full list."""
    if value is None:
        return None
    parts = str(value).split(separator)
    if index is None:
        return parts
    return parts[index]


_UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


@register_transform("parse_datetime", primary="format")
def parse_datetime(value: Any, *, format: str, tz_offset_hours: float = 0.0) -> int | None:
    """Parse a formatted datetime string to Unix nanoseconds.

    ``format`` is a strptime pattern (e.g. ``"%d.%m.%Y %H:%M:%S.%f"``). A value with
    no zone is assumed to be at ``tz_offset_hours`` (default 0 = UTC); a pattern with
    ``%z`` is honoured as parsed. Many forensic exports give formatted strings rather
    than epochs, so this is the temporal counterpart of ``arithmetic``.
    """
    if value is None:
        return None
    parsed = dt.datetime.strptime(str(value), format)
    if parsed.tzinfo is None:
        # WHY: a naive timestamp is meaningless without a zone; the preset declares it,
        # defaulting to UTC, rather than the engine silently assuming local time.
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=tz_offset_hours)))
    # Integer nanoseconds, exact: datetime resolves to microseconds, so ns = us * 1000
    # (avoids the float error of multiplying timestamp() by 1e9).
    delta = parsed - _UNIX_EPOCH
    microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return microseconds * 1000
