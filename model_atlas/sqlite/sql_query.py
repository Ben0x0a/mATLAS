"""Validation and read-only hardening for user-supplied custom SQL queries.

Defines: ``CustomSQLError`` (raised on a rejected query), ``validate_custom_sql``
(parser-free syntactic gate), and ``harden_connection`` (applies the
table-qualified column naming + a read-only SQLite authorizer to a connection).

Used by: ``model_atlas.sqlite.extractor`` for two-pass extraction
of a custom query defined in a source preset.

Uses: stdlib ``re`` and ``sqlite3``.

Design decisions
----------------
SQL injection is explicitly NOT in scope: the analyst already has full read
access to the database, so there is nothing to escalate to. The goal of the
gate is narrower and forensic:

1. *Never crash the application* on a malformed or mutating query — so we
   restrict to a single, read-only ``SELECT``/``WITH`` statement and install a
   SQLite authorizer that denies anything other than reads.
2. *Stay faithful to the original data* — so output-column renaming is
   forbidden. We reject the ``AS`` keyword outright (this also blocks table
   aliases, accepted as a simplification per the design discussion). The cost
   is that ``CAST(x AS y)`` is rejected too, which is acceptable: casting is a
   transformation and a faithful extractor should not silently retype columns.

Because ``AS`` is forbidden, common table expressions (``WITH name AS (...)``)
are unsupported — their syntax mandates ``AS``. Only a single ``SELECT`` is
accepted; joins (the motivating use case) need no CTE.

The check is deliberately *parser-free* (no SQL-parser dependency). It strips
comments, string literals and quoted identifiers first so a column literally
named ``as`` or a string containing the word ``AS`` cannot trip the keyword
scan, then applies simple word-boundary regexes. This cannot reason about query
structure, so two limitations are accepted by design:

* an *implicit* table alias (``FROM cache c``, no ``AS``) still passes — it
  never changes output column names, so it is harmless to faithfulness;
* the denylist below is a courtesy for clearer early errors — the real
  read-only guarantee is enforced at execution time by ``harden_connection``'s
  authorizer, not by string matching.

Duplicate output columns from joins are disambiguated without renaming by
forcing ``table.column`` naming (``full_column_names``); see
``harden_connection``.
"""
from __future__ import annotations

import re
import sqlite3


class CustomSQLError(ValueError):
    """A custom SQL query was rejected by validation.

    Subclasses ``ValueError`` so the CLI and GUI input paths, which already
    surface ``ValueError`` as a user-facing message, report it uniformly."""


# Comments, string literals and quoted identifiers, matched so they can be
# blanked out before keyword scanning. Order matters: comments first, then the
# three SQLite string/identifier quoting forms (with their doubled-quote escape
# where applicable).
_NOISE_PATTERN = re.compile(
    r"""
      --[^\n]*            # line comment to end of line
    | /\*.*?\*/           # block comment (non-greedy)
    | '(?:[^']|'')*'      # 'string literal' ('' escapes a quote)
    | "(?:[^"]|"")*"      # "quoted identifier"
    | \[[^\]]*\]          # [bracketed identifier]
    | `[^`]*`             # `backtick identifier`
    """,
    re.VERBOSE | re.DOTALL,
)

# Mutating / structural statements rejected up-front for a clear error message.
# Not the security boundary — that is the authorizer in harden_connection.
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE",
    "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX", "TRIGGER",
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "GRANT", "ANALYZE",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE
)
_STARTS_READONLY_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_ALIAS_RE = re.compile(r"\bAS\b", re.IGNORECASE)

# Authorizer actions a read-only SELECT (with joins and functions) legitimately
# needs. Everything else — writes, ATTACH, PRAGMA, DDL — is denied, so a query
# that slipped past the syntactic gate still cannot mutate or reach outside the
# database.
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
)


def _blank_noise(sql: str) -> str:
    """Replace comments/strings/quoted identifiers with equal-length runs of
    spaces. Spaces (rather than removal) preserve token boundaries so e.g.
    ``a'x'b`` does not collapse into a single token."""
    return _NOISE_PATTERN.sub(lambda m: " " * len(m.group(0)), sql)


def validate_custom_sql(sql: str) -> str:
    """Validate a user-supplied query and return it ready to execute.

    Returns the original query with surrounding whitespace and a single
    trailing semicolon removed. Raises :class:`CustomSQLError` (a ``ValueError``)
    with a human-readable reason on any rejection. This is a syntactic gate
    only; ``harden_connection`` enforces read-only execution.
    """
    if not isinstance(sql, str):
        raise CustomSQLError(f"SQL must be text, got {type(sql).__name__}")
    cleaned = sql.strip()
    if not cleaned:
        raise CustomSQLError("SQL query is empty.")
    # Drop a single trailing ';' so a normal "SELECT ...;" is one statement.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise CustomSQLError("SQL query is empty.")

    scan = _blank_noise(cleaned)

    # Single statement: any ';' left after blanking strings/comments means a
    # second statement was appended. Guard consequence: executing multiple
    # statements could run a hidden mutation after a benign SELECT.
    if ";" in scan:
        raise CustomSQLError(
            "Only a single SQL statement is allowed (remove the ';' and any "
            "statement after it)."
        )
    # Read-only shape: must begin with SELECT. WITH (CTEs) are unsupported
    # because their syntax requires AS, which is forbidden below.
    if not _STARTS_READONLY_RE.match(scan):
        raise CustomSQLError(
            "Only read-only SELECT queries are allowed; the statement must "
            "start with SELECT."
        )
    # Faithfulness: no AS — forbids renaming output columns (and, by
    # simplification, table aliases). Keeps headers identical to the source.
    if _ALIAS_RE.search(scan):
        raise CustomSQLError(
            "Renaming with AS is not allowed (columns must keep their original "
            "names). Remove the AS clause; duplicate join columns are "
            "disambiguated automatically as table.column."
        )
    match = _FORBIDDEN_RE.search(scan)
    if match:
        raise CustomSQLError(
            f"Statement keyword {match.group(1).upper()!r} is not allowed; only "
            "read-only SELECT queries are supported."
        )
    return cleaned


def _readonly_authorizer(
    action: int,
    arg1: object,
    arg2: object,
    db_name: object,
    trigger_or_view: object,
) -> int:
    """SQLite authorizer callback: permit reads, deny everything else.

    Returning ``SQLITE_DENY`` makes SQLite raise ``OperationalError`` at prepare
    time, which the extraction/preview paths catch — so a denied operation is a
    handled error, never a crash or a mutation of the working copy."""
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def harden_connection(conn: sqlite3.Connection) -> None:
    """Prepare ``conn`` to run a custom query read-only with faithful column
    names.

    Two effects, applied in this order (the pragmas must run *before* the
    authorizer, which would otherwise deny PRAGMA itself):

    1. ``short_column_names=OFF`` + ``full_column_names=ON`` make SQLite report
       every output column as ``table.column``. This disambiguates duplicate
       names from joins (e.g. two ``Z_PK``) without an AS rename — the
       qualifier is the real table/alias name, so no name is invented.
    2. ``set_authorizer`` installs the read-only gate described above.
    """
    conn.execute("PRAGMA short_column_names = OFF")
    conn.execute("PRAGMA full_column_names = ON")
    conn.set_authorizer(_readonly_authorizer)
