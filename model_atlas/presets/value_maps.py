"""Value-key normalisation for the lookup pipe transform.

Defines:    value_map_key — normalise a source value to a canonical lookup key so YAML
            integer and string keys (1 and "1") match equivalently.
Used by:    presets.pipes (the lookup transform).
Depends on: standard library only.

T60 fold: moved verbatim from ``transforms/value_maps.py`` — the lookup
tables are preset data, so their key normalisation lives with the preset pipes.
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
