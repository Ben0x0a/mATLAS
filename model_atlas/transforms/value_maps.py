"""Value-key normalisation for the lookup transform.

Defines:    value_map_key — normalise a source value to a canonical lookup key so YAML
            integer and string keys (1 and "1") match equivalently.
Used by:    transforms.builtin (the lookup transform).
Depends on: standard library only.
"""
from __future__ import annotations

import numbers
from typing import Any


def value_map_key(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(value)
    if isinstance(value, numbers.Real) and float(value).is_integer():
        return str(int(value))
    return str(value)
