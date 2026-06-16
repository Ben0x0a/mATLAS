"""Shared SQLite helpers for the source-agnostic SQLite adapter.

Defines a single, strict identifier-quoting routine used by
``model_atlas.sqlite.extractor`` so every table-name interpolation
goes through the same validation path.
"""
from __future__ import annotations


def quote_identifier(identifier: str) -> str:
    """Return ``identifier`` safely quoted for use in a SQLite statement.

    PRAGMA and DDL paths in SQLite do not accept bound parameters for object
    names. We therefore inline the name, but only after asserting it cannot
    break out of the surrounding double-quotes. Embedded double-quotes are
    rejected outright (rather than escaped by doubling) because a forensic tool
    that needs to handle a table literally named ``foo")bar`` is exceedingly
    unlikely — and refusing is a clearer signal than silently mutating the
    identifier.
    """
    if not isinstance(identifier, str):
        raise TypeError(f"identifier must be str, got {type(identifier).__name__}")
    if not identifier:
        raise ValueError("identifier must not be empty")
    if '"' in identifier:
        raise ValueError(f"Invalid identifier (contains quote): {identifier!r}")
    if "\x00" in identifier:
        raise ValueError(f"Invalid identifier (contains NUL): {identifier!r}")
    if any(ord(c) < 0x20 for c in identifier):
        raise ValueError(f"Invalid identifier (contains control char): {identifier!r}")
    return f'"{identifier}"'
