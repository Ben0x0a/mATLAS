"""The evidence-protected edit API for a persisted mATLAS run database.

Defines:    ``EditApi`` — opens an ALREADY-PERSISTED ``<name>.matlas.sqlite`` read-write,
            lets a caller navigate rows and mutate them under the evidence-protection
            rules, and keeps the rewindable ``edit_log`` journal. The public surface is
            ``open`` / ``tables`` / ``rows`` / ``row`` / ``update`` / ``save_as_new_row`` /
            ``add_empty_row`` / ``rewind`` / ``edit_log`` / ``close``. All protection is
            enforced HERE (never in the UI): ``update`` refuses any L0/L1 evidence table and
            points the caller at ``save_as_new_row``; only the integration/analysis tables
            may be updated in place.
Used by:    ``gui.db_edit_controller`` (the Database Edit panel calls this and nothing else
            touches the run DB from the GUI) and ``tests.app.test_edit_api``.
Depends on: ``model_atlas.core.store`` (``RunStore.open_persisted`` — which opens and
            authenticates the run DB — and the L0-L3 layer partition, the single source of
            truth for which tables are editable in place), ``model_atlas.core.schema``
            (``COPYABLE_TABLES`` — the editable/navigable table set), ``model_atlas.core.ids``
            (``canonical_json`` for the full-row snapshots and ``id_for`` for minting fresh
            occurrence ids) and the standard library only (``sqlite3``, ``re``, ``json``,
            ``getpass``, ``datetime``).

Editable-table set (single source of truth)
--------------------------------------------
The editable tables are derived from the store's layer partition rather than re-listed, so
they cannot drift from the model: everything in L2 (the integrations + the spatial index)
and L3 EXCEPT ``edit_log`` itself (the journal is never edited as evidence). That yields
exactly ``{spatial_integration, timespan_integration, spatial_index, analysis}`` — the
integration/analysis rows the model declares mutable in place. Every other
table is L0/L1 evidence: an ``update`` on it RAISES and directs the caller to
``save_as_new_row``, the one allowed edit path for evidence (mint a new occurrence, keep the
original untouched).

Rewind mechanism
----------------
``update`` logs one entry per column carrying the JSON-encoded old->new scalar;
``new_row`` / ``empty_row`` log the FULL row snapshot as canonical JSON in ``new_value``
(``old_value`` NULL — there was no prior row). ``ordinal`` is a monotonic UNIQUE journal
position. ``rewind(to_ordinal)`` replays the inverse of every entry with a HIGHER ordinal,
in DESCENDING ordinal order, and APPENDS each inverse as a fresh logged entry (the journal
is never erased):

* inverse of ``update``    -> ``SET column = old`` (old/new swapped in the appended entry);
* inverse of ``new_row`` / ``empty_row`` -> DELETE the created row; the appended entry
  carries the snapshot in ``old_value`` and a NULL ``new_value`` (the symmetric mirror of a
  creation, whose ``new_value`` holds the snapshot). So a ``new_row``/``empty_row`` entry
  reads "row present with this snapshot" when ``new_value`` is set and "row absent" when it
  is NULL — the exact reverse of the forward operation.

Scalars are JSON-encoded in the log (``ids.canonical_json``) rather than stored as bare
text so a rewind restores the EXACT typed value (a REAL stays a float, a NULL stays None),
never a string that SQLite would silently coerce into a REAL column.

Id minting for new occurrences
-------------------------------
A manually created row is an operator artefact with no model natural key, so its id is
derived deterministically from the audit event that created it: ``id_for(table,
<origin>, ordinal)`` where ``ordinal`` is the monotonic UNIQUE journal position about to be
assigned. Uniqueness is guaranteed by the ordinal's UNIQUE constraint; determinism keeps
the derivation inside ``core.ids``' namespace/separator discipline. ``save_as_new_row``
seeds the origin with the original row id (the occurrence it was cloned from);
``add_empty_row`` uses a fixed ``"empty_row"`` tag.
"""
from __future__ import annotations

import getpass
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from model_atlas.core import ids
from model_atlas.core import schema
from model_atlas.core import store as store_mod
from model_atlas.core import vocabularies as V

# The tables an ``update`` may touch in place, derived from the store's layer partition so
# there is ONE source of truth for the L0/L1-vs-L2/L3 split (a table added to a layer set in
# the store automatically follows here). ``edit_log`` is L3 but is the journal itself, never
# edited as evidence, so it is excluded.
EDITABLE_TABLES: frozenset[str] = (
    frozenset(store_mod.L2_TABLES) | frozenset(store_mod.L3_TABLES)
) - {"edit_log"}


class EditApi:
    """Read-write access to a persisted mATLAS run DB under the evidence-protection
    and rewind-journal edit rules.

    Open with :meth:`open` (which authenticates the file), use as a context manager so the
    connection is always closed::

        with EditApi.open("case.matlas.sqlite") as api:
            api.update("spatial_integration", si_id, "latitude_wgs84", 46.20)
    """

    def __init__(self, store: "store_mod.RunStore", *, who: str | None = None) -> None:
        # ``who`` stamps every edit_log row; default to the OS user so an audit trail always
        # names an actor even when the caller does not pass one.
        self._store = store
        self._conn = store.connection
        self._conn.row_factory = sqlite3.Row
        self._who = who or getpass.getuser()

    # --- opening + authentication -----------------------------------------

    @classmethod
    def open(cls, path: str | Path, *, who: str | None = None) -> "EditApi":
        """Open ``path`` read-write and REJECT anything that is not a real mATLAS run DB.

        The store's ``open_persisted`` attaches to the existing run database, turns on FK
        enforcement and authenticates the versioning stamps + ``matlas_meta`` marker (a
        missing file raises ``FileNotFoundError``; a foreign/wrong-generation DB raises
        ``ValueError``) — so opening the wrong file can never mutate an unrelated database.
        """
        store = store_mod.RunStore.open_persisted(path)
        return cls(store, who=who)

    def close(self) -> None:
        """Close the underlying store (the persisted DB file is kept — it is evidence)."""
        self._store.close()

    def __enter__(self) -> "EditApi":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- navigation (read-only) -------------------------------------------

    def tables(self) -> tuple[str, ...]:
        """The evidence/derived tables in ingest order (matlas_meta + views excluded)."""
        return schema.COPYABLE_TABLES

    def rows(self, table: str) -> list[dict[str, Any]]:
        """Every row of ``table`` as plain dicts, ordered by its primary key for stability."""
        self._require_table(table)
        pk = self._pk(table)
        cursor = self._conn.execute(f"SELECT * FROM {table} ORDER BY {pk}")
        return [dict(row) for row in cursor.fetchall()]

    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        """One row by primary-key value, or ``None`` if it does not exist."""
        self._require_table(table)
        pk = self._pk(table)
        found = self._conn.execute(
            f"SELECT * FROM {table} WHERE {pk} = ?", (row_id,)).fetchone()
        return dict(found) if found is not None else None

    def edit_log(self) -> list[dict[str, Any]]:
        """The full audit journal as dicts, in ascending ordinal order (never filtered)."""
        cursor = self._conn.execute("SELECT * FROM edit_log ORDER BY ordinal")
        return [dict(row) for row in cursor.fetchall()]

    # --- mutation (protection enforced here) ------------------------------

    def update(self, table: str, row_id: str, column: str, value: Any) -> None:
        """Edit ONE column of an integration/analysis row in place; log old->new in the
        rewindable edit journal.

        Raises ``PermissionError`` on any L0/L1 evidence table (directing the caller to
        ``save_as_new_row``) — the protection lives here, not in the UI, so no caller can
        bypass it. Also raises on an unknown table/column or an attempt to edit the primary
        key (which would forge identity and orphan the audit row_id).
        """
        self._require_table(table)
        if table not in EDITABLE_TABLES:
            layer = store_mod.TABLE_LAYER.get(table, "?")
            raise PermissionError(
                f"{table!r} is {layer} evidence and is never edited in place "
                f"(decision 34); call save_as_new_row({table!r}, ...) to mint a new "
                f"occurrence carrying the change and keep the original untouched")
        self._require_column(table, column)
        pk = self._pk(table)
        # Guard: editing the PK would break the FK graph and detach this row from its audit
        # ordinal — an evidence-integrity violation, so it is rejected rather than logged.
        if column == pk:
            raise ValueError(f"the primary key {column!r} of {table!r} cannot be edited")
        current = self.row(table, row_id)
        if current is None:
            raise KeyError(f"no row {row_id!r} in {table!r}")
        old = current[column]
        self._conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE {pk} = ?", (value, row_id))
        self._append_log(table=table, row_id=row_id, column=column,
                         old_value=_enc(old), new_value=_enc(value),
                         edit_kind=V.EditKind.UPDATE)
        self._conn.commit()

    def save_as_new_row(self, table: str, row_id: str, edits: dict[str, Any]) -> str:
        """Clone ``row_id`` with ``edits`` applied as a NEW occurrence; original untouched.

        The one allowed edit path for L0/L1 evidence (and usable on any table). Mints a fresh
        deterministic id (see the module header), inserts the cloned+edited row, and logs a
        ``new_row`` entry carrying the FULL row snapshot as canonical JSON. Returns the new id.
        """
        self._require_table(table)
        original = self.row(table, row_id)
        if original is None:
            raise KeyError(f"no row {row_id!r} in {table!r}")
        for column in edits:
            self._require_column(table, column)
        pk = self._pk(table)
        ordinal = self._next_ordinal()
        new_id = ids.id_for(table, row_id, ordinal)
        snapshot = dict(original)
        snapshot.update(edits)
        snapshot[pk] = new_id   # the new occurrence carries its own fresh identity
        self._insert(table, snapshot)
        self._append_log(table=table, row_id=new_id, column=None,
                         old_value=None, new_value=ids.canonical_json(snapshot),
                         edit_kind=V.EditKind.NEW_ROW, ordinal=ordinal)
        self._conn.commit()
        return new_id

    def add_empty_row(self, table: str) -> str:
        """Insert a blank row (fresh id) and log an ``empty_row`` snapshot; return the new id.

        HOW: a fresh pk id, every nullable column left NULL, and every NOT NULL column filled
        with a valid placeholder (an enum column takes its first constrained value, otherwise
        an empty string / 0 / 0.0 by declared type). WHY placeholders rather than pure NULLs:
        a table's NOT NULL / CHECK constraints would reject an all-null row, so a genuinely
        insertable blank is the minimal constraint-satisfying row. A NOT NULL foreign-key
        column cannot be fabricated (a blank row has no real parent), so such a table RAISES.
        """
        self._require_table(table)
        pk = self._pk(table)
        ordinal = self._next_ordinal()
        new_id = ids.id_for(table, "empty_row", ordinal)
        snapshot = self._blank_row(table, pk, new_id)
        self._insert(table, snapshot)
        self._append_log(table=table, row_id=new_id, column=None,
                         old_value=None, new_value=ids.canonical_json(snapshot),
                         edit_kind=V.EditKind.EMPTY_ROW, ordinal=ordinal)
        self._conn.commit()
        return new_id

    def rewind(self, to_ordinal: int) -> None:
        """Undo every edit after ``to_ordinal`` by appending its inverse (a full rewind).

        HOW: snapshot the journal entries with ``ordinal > to_ordinal`` (only those present
        BEFORE the rewind — appended inverses get higher ordinals and are excluded), then
        apply their inverses in DESCENDING ordinal order, appending each as a fresh logged
        entry. WHY append rather than delete: the journal is a complete, totally ordered audit
        record; a rewind is itself an auditable event, so it extends the journal and never
        erases history.
        """
        pending = self._conn.execute(
            "SELECT ordinal, table_name, row_id, column, old_value, new_value, edit_kind "
            "FROM edit_log WHERE ordinal > ? ORDER BY ordinal DESC",
            (to_ordinal,)).fetchall()
        for entry in pending:
            self._invert(entry)
        self._conn.commit()

    # --- inverse application ----------------------------------------------

    def _invert(self, entry: sqlite3.Row) -> None:
        """Apply and log the inverse of one journal entry (see the module header)."""
        table = entry["table_name"]
        row_id = entry["row_id"]
        kind = entry["edit_kind"]
        if kind == V.EditKind.UPDATE.value:
            # Inverse of a column edit: restore the old scalar; the appended entry swaps
            # old/new so the journal reads as a symmetric edit back to the prior value.
            column = entry["column"]
            restored = _dec(entry["old_value"])
            self._conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE {self._pk(table)} = ?",
                (restored, row_id))
            self._append_log(table=table, row_id=row_id, column=column,
                             old_value=entry["new_value"], new_value=entry["old_value"],
                             edit_kind=V.EditKind.UPDATE)
        else:
            # Inverse of a creation (new_row / empty_row): remove the created row. The
            # appended entry carries the snapshot in old_value with a NULL new_value — the
            # mirror of a creation, whose new_value holds the snapshot.
            self._conn.execute(
                f"DELETE FROM {table} WHERE {self._pk(table)} = ?", (row_id,))
            self._append_log(table=table, row_id=row_id, column=None,
                             old_value=entry["new_value"], new_value=None,
                             edit_kind=V.EditKind(kind))

    # --- write helpers ----------------------------------------------------

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        """INSERT a full row dict into ``table`` (columns absent from the dict stay default)."""
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(row[c] for c in columns))

    def _append_log(self, *, table: str | None, row_id: str | None, column: str | None,
                    old_value: str | None, new_value: str | None,
                    edit_kind: V.EditKind, ordinal: int | None = None) -> None:
        """Append one ``edit_log`` row (fresh uuid4 edit_id, next monotonic ordinal, who/when).

        ``ordinal`` may be pre-reserved by the caller (so a new-row's minted id and its log
        entry share the same journal position); otherwise the next ordinal is claimed here.
        """
        if ordinal is None:
            ordinal = self._next_ordinal()
        self._conn.execute(
            "INSERT INTO edit_log (edit_id, ordinal, table_name, row_id, column, "
            "old_value, new_value, edit_kind, edited_by, edited_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ids.edit_log_id(), ordinal, table, row_id, column, old_value, new_value,
             edit_kind.value, self._who, datetime.now(timezone.utc).isoformat()))

    def _next_ordinal(self) -> int:
        """The next monotonic journal position: ``MAX(ordinal) + 1`` (1 for an empty log)."""
        current = self._conn.execute("SELECT MAX(ordinal) FROM edit_log").fetchone()[0]
        return 1 if current is None else current + 1

    def _blank_row(self, table: str, pk: str, new_id: str) -> dict[str, Any]:
        """Build the minimal constraint-satisfying blank row for ``add_empty_row``."""
        fk_columns = self._fk_columns(table)
        row: dict[str, Any] = {pk: new_id}
        for info in self._conn.execute(f"PRAGMA table_info({table})").fetchall():
            name, decl_type, not_null, is_pk = (
                info["name"], info["type"], info["notnull"], info["pk"])
            if is_pk or not not_null:
                continue   # pk is set; a nullable column stays absent (NULL)
            if name in fk_columns:
                raise ValueError(
                    f"cannot add a blank {table!r} row: the NOT NULL foreign key {name!r} "
                    f"has no parent to reference; add the parent row first")
            row[name] = self._blank_value(table, name, decl_type)
        return row

    def _blank_value(self, table: str, column: str, decl_type: str) -> Any:
        """A valid placeholder for a NOT NULL column with no real value yet."""
        allowed = self._check_values(table, column)
        if allowed:
            return allowed[0]   # a constrained (enum) column: its first legal token
        upper = decl_type.upper()
        if "INT" in upper:
            return 0
        if "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
            return 0.0
        return ""

    # --- schema introspection (live DB only, no schema.py internals) ------

    def _require_table(self, table: str) -> None:
        """Reject an unknown table name loudly (also blocks SQL injection via the name)."""
        if table not in schema.COPYABLE_TABLES:
            raise ValueError(f"unknown run-DB table: {table!r}")

    def _require_column(self, table: str, column: str) -> None:
        if column not in self._columns(table):
            raise ValueError(f"{table!r} has no column {column!r}")

    def _columns(self, table: str) -> tuple[str, ...]:
        return tuple(info["name"]
                     for info in self._conn.execute(f"PRAGMA table_info({table})"))

    def _pk(self, table: str) -> str:
        """The single primary-key column of ``table`` (every model table has exactly one)."""
        for info in self._conn.execute(f"PRAGMA table_info({table})"):
            if info["pk"]:
                return info["name"]
        raise AssertionError(f"table {table!r} has no primary key")  # schema invariant

    def _fk_columns(self, table: str) -> set[str]:
        return {info["from"]
                for info in self._conn.execute(f"PRAGMA foreign_key_list({table})")}

    def _check_values(self, table: str, column: str) -> list[str]:
        """The ``CHECK (col IN ('a','b'...))`` value list for ``column``, parsed from the DDL.

        WHY parse the live DDL rather than import the schema's enum map: it keeps this API
        decoupled from ``core.schema``'s internals and honours whatever the OPENED database
        actually constrains (a future schema generation cannot silently diverge here).
        """
        ddl = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)).fetchone()
        if ddl is None or ddl["sql"] is None:
            return []
        match = re.search(rf"CHECK \({re.escape(column)} IN \(([^)]*)\)", ddl["sql"])
        if match is None:
            return []
        return re.findall(r"'([^']*)'", match.group(1))


# --- log value encoding ----------------------------------------------------

def _enc(value: Any) -> str:
    """JSON-encode a scalar for the log so a rewind restores the EXACT typed value.

    A REAL stays a float, an INTEGER an int, a NULL the JSON ``null`` (distinct from an
    absent/omitted value, which is a genuine SQL NULL in the log column).
    """
    return ids.canonical_json(value)


def _dec(encoded: str) -> Any:
    """Inverse of :func:`_enc`: recover the typed scalar from its JSON encoding."""
    return json.loads(encoded)
