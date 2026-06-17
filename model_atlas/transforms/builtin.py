"""Built-in transforms, timestamp codecs, and unit/cast helpers for v3.

Defines:    the pipe transforms (cast, scale, arithmetic, lookup, regex, split),
            the timezone-offset parser, the string datetime parser, and the named
            epoch decoders (cocoa/unix_*/webkit) -> Unix nanoseconds.
Used by:    transforms.assemble (declarative cast/unit/epoch + procedural pipe) via
            transforms.registry.
Depends on: transforms.expression (sandbox), transforms.value_maps (key normaliser),
            transforms.registry, model.families (cast inference + unit factors).

Every transform treats ``None`` as a no-op so an empty source cell flows through a
pipe untouched rather than raising.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from model_atlas.transforms.expression import evaluate
from model_atlas.transforms.registry import PipeContext, TransformHardError, register_transform
from model_atlas.transforms.value_maps import value_map_key

_TRUE_TOKENS = {"1", "true", "yes", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "n", "f", ""}

_UNIX_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

# Seconds between the Unix epoch (1970-01-01) and the Cocoa/Core-Data epoch (2001-01-01)
# and the WebKit epoch (1601-01-01).
_COCOA_OFFSET_S = 978_307_200
_WEBKIT_OFFSET_S = -11_644_473_600

# epoch name -> (scale to seconds, offset in seconds added AFTER scaling to seconds).
_EPOCHS: dict[str, tuple[float, float]] = {
    "unix_s": (1.0, 0.0),
    "unix_ms": (1e-3, 0.0),
    "unix_us": (1e-6, 0.0),
    "unix_ns": (1e-9, 0.0),
    "cocoa": (1.0, _COCOA_OFFSET_S),
    "webkit": (1e-6, _WEBKIT_OFFSET_S),  # WebKit/Chrome microseconds since 1601
}


def _to_int(value: Any) -> int:
    return int(value)


# --- timezone + datetime ----------------------------------------------------

_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})?$")


def tz_offset_to_hours(value: Any) -> float:
    """Normalise a timezone offset (number or string) to signed decimal hours."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    token = str(value).strip().upper()
    if token in ("", "UTC", "GMT", "Z"):
        return 0.0
    for prefix in ("UTC", "GMT"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    match = _TZ_OFFSET_RE.match(token)
    if match is None:
        raise ValueError(f"Unrecognised timezone offset {value!r}")
    sign = 1 if match.group(1) == "+" else -1
    return sign * (int(match.group(2)) + int(match.group(3) or 0) / 60)


def parse_datetime_to_ns(value: Any, fmt: str, tz_offset_hours: Any = 0.0) -> int | None:
    """Parse a formatted datetime string to Unix nanoseconds (None -> None).

    A naive value is interpreted at ``tz_offset_hours`` (number or offset string); a
    ``%z`` in the format is honoured as parsed.
    """
    if value is None:
        return None
    parsed = dt.datetime.strptime(str(value), fmt)
    if parsed.tzinfo is None:
        offset = tz_offset_to_hours(tz_offset_hours)
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=offset)))
    delta = parsed - _UNIX_EPOCH
    microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return microseconds * 1000


def epoch_to_ns(value: Any, epoch: str) -> int | None:
    """Decode a numeric epoch value under a named encoding to Unix nanoseconds.

    Self-documents the conversion a preset would otherwise hand-roll: e.g. ``cocoa``
    adds the 2001->1970 offset; ``unix_ms`` scales milliseconds; etc.
    """
    if value is None:
        return None
    if epoch not in _EPOCHS:
        raise TransformHardError(f"Unknown epoch {epoch!r}")
    scale, offset_s = _EPOCHS[epoch]
    seconds = float(value) * scale + offset_s
    # Round to the nearest nanosecond; float precision is ample for forensic seconds.
    return int(round(seconds * 1_000_000_000))


# --- pipe transforms (value, args, kwargs, ctx) -----------------------------

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
