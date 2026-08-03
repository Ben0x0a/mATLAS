"""Value-key normalisation and scalar coercion shared across the preset resolvers.

Defines:    value_map_key — normalise a source value to a canonical lookup key so YAML
            integer and string keys (1 and "1") match equivalently; and ``to_bool`` — the
            single boolean-cast rule used by BOTH the ``cast`` pipe and ``coerce_scalar``.
Used by:    presets.pipes (the ``cast``/``lookup`` transforms) and presets.resolve
            (``coerce_scalar``).
Depends on: standard library only.

Moved verbatim from ``transforms/value_maps.py`` — the lookup
tables are preset data, so their key normalisation lives with the preset pipes.
"""
from __future__ import annotations

import numbers
from typing import Any

# Recognised string spellings of a boolean (matched case-insensitively, stripped). WHY a
# single source: ``bool("false")`` is True (any non-empty string is truthy), so a bare
# ``bool()`` cast would silently invert a source string; both the cast pipe and coerce_scalar
# must reject an unrecognised token rather than guess a truth value for evidence.
_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "t"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "f", ""})


def to_bool(value: Any) -> bool:
    """Cast a source scalar to bool by its recognised spelling, never ``bool()``'s truthiness.

    HOW: stringify, strip, casefold, then match the true/false token sets; an UNRECOGNISED
    token RAISES ``ValueError`` (a forensic cast must not fabricate a truth value). A real
    bool round-trips (``str(True)`` -> ``"true"``), as do 0/1 and their string forms.
    """
    token = str(value).strip().casefold()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"cannot cast {value!r} to bool")


def value_map_key(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(value)
    if isinstance(value, numbers.Real) and float(value).is_integer():
        return str(int(value))
    return str(value)
