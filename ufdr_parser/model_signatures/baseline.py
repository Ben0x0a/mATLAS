"""SQLite baseline of a Cellebrite report's decoded-data shape.

Defines:    Baseline — a disk-backed store of the known model types, relations, and field
            names, each stamped with first-seen/last-seen times.
Used by:    drift (reads the known sets, records the observed shape).
Depends on: standard library sqlite3/datetime.

Schema (one table per concept; ``level`` 0/1/2 distinguishes model / submodel /
sub-submodel, replacing the legacy ``Models``/``SubModels``/``SubSubModels`` split):
  * ``model_types(model_type, level, detected, last_seen)``
  * ``relations(child, parent, detected, last_seen)``
  * ``fields(model_type, level, field_name, detected, last_seen)``
``detected`` is set once when a row first appears; ``last_seen`` is bumped every run.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS model_types ("
    "model_type TEXT, level INTEGER, detected TEXT, last_seen TEXT, "
    "PRIMARY KEY (model_type, level))",
    "CREATE TABLE IF NOT EXISTS relations ("
    "child TEXT, parent TEXT, detected TEXT, last_seen TEXT, "
    "PRIMARY KEY (child, parent))",
    "CREATE TABLE IF NOT EXISTS fields ("
    "model_type TEXT, level INTEGER, field_name TEXT, detected TEXT, last_seen TEXT, "
    "PRIMARY KEY (model_type, level, field_name))",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Baseline:
    """The known-good decoded-data shape, persisted in a SQLite file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    # --- reads ---------------------------------------------------------------

    def is_empty(self) -> bool:
        """True when no model types are recorded yet (a fresh baseline to establish)."""
        row = self._conn.execute("SELECT 1 FROM model_types LIMIT 1").fetchone()
        return row is None

    def known_model_types(self) -> set[tuple[int, str]]:
        return {
            (level, model_type)
            for model_type, level in self._conn.execute(
                "SELECT model_type, level FROM model_types"
            )
        }

    def known_relations(self) -> set[tuple[str, str]]:
        return set(self._conn.execute("SELECT child, parent FROM relations"))

    def known_fields(self) -> set[tuple[int, str, str]]:
        return {
            (level, model_type, field_name)
            for model_type, level, field_name in self._conn.execute(
                "SELECT model_type, level, field_name FROM fields"
            )
        }

    # --- writes (upsert: insert with detected, else bump last_seen) -----------

    def record_model_type(self, level: int, model_type: str) -> None:
        self._upsert(
            "model_types", ("model_type", "level"), (model_type, level)
        )

    def record_relation(self, child: str, parent: str) -> None:
        self._upsert("relations", ("child", "parent"), (child, parent))

    def record_field(self, level: int, model_type: str, field_name: str) -> None:
        self._upsert(
            "fields",
            ("model_type", "level", "field_name"),
            (model_type, level, field_name),
        )

    def _upsert(self, table: str, key_cols: tuple[str, ...], key_vals: tuple) -> None:
        # Table/column names are module-internal literals (never user input), so the
        # f-string here cannot carry injection; values are still bound parameters.
        now = _now()
        where = " AND ".join(f"{col} = ?" for col in key_cols)
        exists = self._conn.execute(
            f"SELECT 1 FROM {table} WHERE {where}", key_vals
        ).fetchone()
        if exists is None:
            cols = ", ".join((*key_cols, "detected", "last_seen"))
            placeholders = ", ".join("?" * (len(key_vals) + 2))
            self._conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                (*key_vals, now, now),
            )
        else:
            self._conn.execute(
                f"UPDATE {table} SET last_seen = ? WHERE {where}", (now, *key_vals)
            )

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
