"""Load the workbook configuration (``ufdr_config.toml``): which sheets, columns and sources.

Defines:    IdentifierSource, WorkbookConfig, CONFIG_FILENAME, default_config_text() and
            load_workbook_config().
Used by:    workbook.brick (loads it once per run), workbook.sheets (reads it through the
            Context), ufdr_parser.cli (init-config writes the default; --config picks a file).
Depends on: default_ufdr_config.toml (package data), stdlib tomllib / importlib.resources.

WHERE the configuration comes from, first match wins:
1. an explicit path (the CLI's ``--config``) — it must exist;
2. ``./ufdr_config.toml`` in the directory mATLAS runs from (the GUI's case);
3. the built-in default shipped in this package (``default_ufdr_config.toml``).
A user file need not be complete: every top-level section it contains REPLACES the default's,
the others fall back to the default. WHY section-level rather than key-level merging: a
section is one coherent choice (a list of sheets, a list of identifier sources); merging two
lists element-wise would silently keep defaults the operator meant to remove.

WHY strict validation: an unknown section, key or sheet name is a typo, and a typo silently
ignored means a workbook missing what the operator asked for. It is reported before the dump
starts (see WorkbookBrick.validate), not after a long parse.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

CONFIG_FILENAME = "ufdr_config.toml"
_DEFAULT_RESOURCE = "default_ufdr_config.toml"

# Allowed keys per section ("identifiers" is an array of tables, validated per entry).
_SECTION_KEYS: dict[str, set[str] | None] = {
    "workbook": {"sheets"},
    "hide_columns": None,  # free keys: sheet names, checked against the sheet registry
    "contacts": {"child_models"},
    "locations": {"record_types"},
    "identifiers": None,
}
_IDENTIFIER_KEYS = {"model", "field", "kind", "kind_field", "only"}


@dataclass(frozen=True)
class IdentifierSource:
    """One place identifiers are collected from: a field of a model."""

    model: str
    field: str
    kind: str | None = None
    kind_field: str | None = None
    # field -> accepted values; a record is kept only when every listed field matches.
    only: dict[str, frozenset[str]] = field(default_factory=dict)  # mutable default — per instance

    def kind_of(self, record: dict[str, object]) -> str:
        """The Kind label of an identifier read from ``record``."""
        if self.kind_field and record.get(self.kind_field):
            return str(record[self.kind_field])
        return self.kind or f"{self.model}.{self.field}"

    def accepts(self, record: dict[str, object]) -> bool:
        return all(record.get(name) in values for name, values in self.only.items())


@dataclass(frozen=True)
class WorkbookConfig:
    sheets: tuple[str, ...]
    hide_columns: dict[str, frozenset[str]]
    contact_child_models: tuple[str, ...]
    location_record_types: tuple[str, ...]
    identifiers: tuple[IdentifierSource, ...]
    # Where it was loaded from, for the About sheet.
    source: str = "built-in default"


def default_config_text() -> str:
    """The built-in default configuration file, verbatim (what init-config writes)."""
    return resources.files("ufdr_parser.bricks.workbook").joinpath(_DEFAULT_RESOURCE).read_text(encoding="utf-8")


def _string_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{CONFIG_FILENAME}: {where} must be a list of strings, got {value!r}")
    return tuple(value)


def _check_keys(table: object, allowed: set[str], where: str) -> dict:
    if not isinstance(table, dict):
        raise ValueError(f"{CONFIG_FILENAME}: {where} must be a table")
    unknown = set(table) - allowed
    if unknown:
        raise ValueError(f"{CONFIG_FILENAME}: unknown key(s) {sorted(unknown)} in {where}; expected {sorted(allowed)}")
    return table


def _identifier_source(entry: object, index: int) -> IdentifierSource:
    where = f"[[identifiers]] #{index + 1}"
    entry = _check_keys(entry, _IDENTIFIER_KEYS, where)
    for required in ("model", "field"):
        if not isinstance(entry.get(required), str) or not entry[required]:
            raise ValueError(f"{CONFIG_FILENAME}: {where} needs a '{required}' string")
    only_raw = entry.get("only", {})
    if not isinstance(only_raw, dict):
        raise ValueError(f"{CONFIG_FILENAME}: {where}.only must be a table of field = [values]")
    only = {name: frozenset(_string_list(values, f"{where}.only.{name}")) for name, values in only_raw.items()}
    for optional in ("kind", "kind_field"):
        if optional in entry and not isinstance(entry[optional], str):
            raise ValueError(f"{CONFIG_FILENAME}: {where}.{optional} must be a string")
    return IdentifierSource(entry["model"], entry["field"], entry.get("kind"), entry.get("kind_field"), only)


def _parse(raw: dict, source: str) -> WorkbookConfig:
    unknown = set(raw) - set(_SECTION_KEYS)
    if unknown:
        raise ValueError(f"{CONFIG_FILENAME}: unknown section(s) {sorted(unknown)}; expected {sorted(_SECTION_KEYS)}")
    for section, keys in _SECTION_KEYS.items():
        if keys is not None:
            _check_keys(raw.get(section, {}), keys, f"[{section}]")
    hide_raw = raw.get("hide_columns", {})
    if not isinstance(hide_raw, dict):
        raise ValueError(f"{CONFIG_FILENAME}: [hide_columns] must be a table")
    identifiers_raw = raw.get("identifiers", [])
    if not isinstance(identifiers_raw, list):
        raise ValueError(f"{CONFIG_FILENAME}: identifiers must be [[identifiers]] blocks")
    return WorkbookConfig(
        sheets=_string_list(raw["workbook"]["sheets"], "[workbook].sheets"),
        hide_columns={
            sheet: frozenset(_string_list(columns, f"[hide_columns].{sheet}")) for sheet, columns in hide_raw.items()
        },
        contact_child_models=_string_list(raw["contacts"]["child_models"], "[contacts].child_models"),
        location_record_types=_string_list(raw["locations"]["record_types"], "[locations].record_types"),
        identifiers=tuple(_identifier_source(entry, i) for i, entry in enumerate(identifiers_raw)),
        source=source,
    )


def load_workbook_config(path: Path | None = None) -> WorkbookConfig:
    """The active configuration: ``path``, else ./ufdr_config.toml, over the built-in default."""
    raw = tomllib.loads(default_config_text())
    source = "built-in default"
    user_path = Path(path) if path is not None else Path.cwd() / CONFIG_FILENAME
    if path is not None and not user_path.is_file():
        raise FileNotFoundError(f"{CONFIG_FILENAME}: {user_path} does not exist")
    if user_path.is_file():
        # TOML forbids a BOM and tomllib rejects one, but Windows editors (Notepad) add it on
        # save; strip it so a hand-edited copy does not fail on an invisible character.
        user = tomllib.loads(user_path.read_bytes().decode("utf-8-sig"))
        unknown = set(user) - set(_SECTION_KEYS)
        if unknown:
            raise ValueError(f"{user_path}: unknown section(s) {sorted(unknown)}; expected {sorted(_SECTION_KEYS)}")
        raw.update(user)  # section-level override (see module docstring)
        source = str(user_path.resolve())
    return _parse(raw, source)
