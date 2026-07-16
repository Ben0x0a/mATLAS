"""Normative determinism rules (run_db_schema.md §"Determinism — normative enumeration
and canonicalisation", R9) as testable functions and constants.

Defines:    rule 1 — ``sqlite_enumeration_sql`` (records enumerate in ``rowid`` order;
            ``WITHOUT ROWID`` tables in primary-key order) and ``numbered_records``
            (recovered rows AFTER all live rows, CONTINUING the same 1-based ``line_no``
            sequence); rule 2 — ``csv_data_line_numbers`` (physical data-line order,
            header excluded, blank lines counted); rule 3 — ``excel_line_no`` (the sheet
            row number itself, so blank rows shift nothing); rule 4 —
            ``canonical_path`` (absolute, NFC, ``/`` separators) and
            ``container_inner_path`` + ``CONTAINER_JOIN`` (the chain-inclusive ``!``
            join for ``inner_path``); rule 5 — ``canonical_json`` (re-exported from
            ``core/ids.py``, the single definition — never duplicated); rule 6 —
            ``ordered_mentions`` (projection aggregation by mention ordinal, then value
            — never rowid); rule 7 — ``BLOB_KEY`` / ``blob_envelope`` (R18: a binary
            cell becomes ``{"$blob": {"sha256": …, "bytes": n}}``; the bytes themselves
            never enter the run DB).
Used by:    the reader/enumeration bricks (they assign ``line_no`` through rules 1-3),
            the discovery walker (rule 4 builds the ``source_id`` natural key), the
            projection/export brick (rule 6), the future UFDR reader (rule 7), and
            tests/core/test_determinism.py + tests/core/test_constraints.py.
Depends on: ``model_atlas.core.ids`` (``canonical_json`` — defined there, reused here)
            and the standard library only (``hashlib``, ``sqlite3``, ``unicodedata``,
            ``collections.abc``, ``enum``).

WHY this module exists: the ids and digests embed ``line_no`` and ``output_ordinal``
(core/ids.py), so diffability is a promise ONLY if enumeration and canonicalisation are
pinned. Each doc rule is one small executable definition here — the reader bricks call
these instead of re-stating the rule, and the tests exercise the doc rule by rule, so a
drifting reader surfaces as a red test, never as silently shifted evidence ids.
"""
from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from enum import Enum

from model_atlas.core.ids import canonical_json  # rule 5 — the ONE definition, reused

__all__ = [
    "BLOB_KEY",
    "CONTAINER_JOIN",
    "blob_envelope",
    "canonical_json",
    "canonical_path",
    "container_inner_path",
    "csv_data_line_numbers",
    "excel_line_no",
    "numbered_records",
    "ordered_mentions",
    "sqlite_enumeration_sql",
]


# --- Rule 1: SQLite enumeration order ---------------------------------------

def _quoted(identifier: str) -> str:
    """SQL-quote an identifier (double quotes, embedded quotes doubled)."""
    return '"' + identifier.replace('"', '""') + '"'


def sqlite_enumeration_sql(connection: sqlite3.Connection, table_name: str) -> str:
    """The normative SELECT enumerating a SQLite table's LIVE records (rule 1).

    HOW: an ordinary table enumerates in ``rowid`` order; a ``WITHOUT ROWID`` table
    (its CREATE TABLE text carries the clause, read from ``sqlite_master``) enumerates
    in primary-key order — PK columns taken in their PK position order from
    ``PRAGMA table_info`` (column 5 is the 1-based PK position, 0 = not part of the PK).
    WHY pinned: ``line_no`` (and through it every record/assertion id) embeds the
    enumeration position — an unpinned order would shift ids between two runs over the
    same evidence and break diffability.
    """
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no such table: {table_name!r}")
    if "WITHOUT ROWID" in (row[0] or "").upper():
        info = connection.execute(f"PRAGMA table_info({_quoted(table_name)})").fetchall()
        pk_columns = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]
        order = ", ".join(_quoted(column) for column in pk_columns)
    else:
        order = "rowid"
    return f"SELECT * FROM {_quoted(table_name)} ORDER BY {order}"


def numbered_records(
    live: Iterable[object], recovered: Iterable[object] = (),
) -> Iterator[tuple[int, object, bool]]:
    """Assign the 1-based ``line_no`` sequence over one source's records (rule 1).

    HOW: live records first, in their enumeration order (rule 1's rowid/PK order for
    SQLite); recovered records (WAL / journal / freelist / carved) AFTER all live rows,
    in the caller's recovery-scan order, CONTINUING the same sequence. Yields
    ``(line_no, record, is_recovered)`` — the flag is what the reader stores as the
    record's ``recovery_state`` marker.
    WHY one continuing sequence: if recovered rows renumbered (or interleaved with) live
    rows, how much a scan happened to recover would shift every following record's
    ``line_no`` — and with it every record/assertion id — between two runs over the
    same evidence.
    """
    line_no = 0
    for record in live:
        line_no += 1
        yield line_no, record, False
    for record in recovered:
        line_no += 1
        yield line_no, record, True


# --- Rule 2: CSV line numbering ----------------------------------------------

def csv_data_line_numbers(
    physical_lines: Iterable[str], *, header_lines: int = 1,
) -> Iterator[tuple[int, str]]:
    """1-based DATA-line numbers over a CSV's physical lines (rule 2).

    HOW: the first ``header_lines`` physical lines are excluded from numbering; every
    later physical line — INCLUDING a blank one — receives the next ``line_no``.
    WHY blanks are counted (and yielded, never dropped): skipping them would renumber
    every following record whenever a blank line appears, shifting ids; counting them
    keeps ``line_no`` a pure physical position, and the reader — not this rule —
    decides what a blank line means.
    """
    if header_lines < 0:
        raise ValueError(f"header_lines must be >= 0; got {header_lines}")
    line_no = 0
    for index, line in enumerate(physical_lines):
        if index < header_lines:
            continue
        line_no += 1
        yield line_no, line


# --- Rule 3: Excel line numbering ---------------------------------------------

def excel_line_no(sheet_row: int) -> int:
    """``line_no`` for an Excel record = the sheet row number itself (rule 3).

    HOW: identity — the 1-based sheet row IS the ``line_no``, so blank rows shift
    nothing (each simply owns its number).
    WHY identity rather than a data-line count (the CSV rule): the sheet grid is an
    addressable physical layout — the row number is already a stable position, and
    counting only non-blank rows would make ``line_no`` depend on how a spreadsheet
    tool chose to materialise empty rows.
    """
    if sheet_row < 1:
        raise ValueError(f"sheet rows are 1-based; got {sheet_row}")
    return sheet_row


# --- Rule 4: path canonicalisation for the source natural key -----------------

# The container-chain join for `inner_path` (rule 4, R10/decision 24). WHY "!" here but
# "\x1f" in the ids: inner_path is a HUMAN-READABLE column (the archive!member notation
# examiners know); the unforgeable machine join lives in ids.SEP where the uuid5 inputs
# are assembled.
CONTAINER_JOIN = "!"


def _has_drive_root(text: str) -> bool:
    """True for a Windows drive-rooted path once separators are normalised (``C:/…``)."""
    return len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] == "/"


def canonical_path(path: str) -> str:
    """Canonicalise a filesystem path for the ``source_id`` natural key (rule 4).

    HOW: NFC-normalise the text, normalise separators to ``/``, then REQUIRE the result
    to be absolute (a POSIX root or a drive-letter root).
    WHY raise on a relative path instead of absolutising: ``os.path.abspath`` would fold
    the process's working directory into the id — non-determinism smuggled into
    evidence identity. Discovery owns absolutisation (it knows the operator's --input
    root); by the time a path feeds an id it must already be absolute.
    WHY NFC: macOS volumes serve decomposed (NFD) names — the same exhibit walked on
    two platforms must derive ONE ``source_id``, not two.
    """
    text = unicodedata.normalize("NFC", path).replace("\\", "/")
    if not (text.startswith("/") or _has_drive_root(text)):
        raise ValueError(f"source paths must be absolute (rule 4): {path!r}")
    return text


def container_inner_path(chain: Sequence[str]) -> str | None:
    """The chain-inclusive ``inner_path``: the container chain joined with ``!`` (rule 4).

    HOW: each element — the outermost container's own name FIRST, then each nested
    member path (R10: the chain is inclusive) — is NFC-normalised with ``/``
    separators, then joined with ``CONTAINER_JOIN``. An empty chain (a loose file) has
    no ``inner_path`` -> None.
    WHY an empty ELEMENT raises: it would silently collapse two different chains onto
    one string ("a!!b" from ("a", "", "b") vs ("a!", "b")) — exactly the forged-key
    class this canonicalisation exists to prevent.
    """
    if not chain:
        return None
    members = []
    for element in chain:
        if not element:
            raise ValueError("container chain elements must be non-empty (rule 4)")
        members.append(unicodedata.normalize("NFC", element).replace("\\", "/"))
    return CONTAINER_JOIN.join(members)


# --- Rule 6: projection aggregation order --------------------------------------

def ordered_mentions(
    mentions: Iterable[tuple[int, object]],
) -> list[tuple[int, object]]:
    """Aggregation order in projections: mention ordinal, then value (rule 6).

    HOW: sort the ``(ordinal, value)`` pairs by ordinal, tie-broken by the value
    rendered as its stable token (an Enum by its bare ``.value``, None as the empty
    string — the same rendering discipline as the id parts in ``core/ids.py``).
    WHY never rowid: rowid is an insertion accident of one particular run DB; an
    aggregation (counterpart, context_mentions, facets) ordered by it would differ
    between two otherwise identical runs, breaking output diffability.
    """
    def _key(mention: tuple[int, object]) -> tuple[int, str]:
        ordinal, value = mention
        if value is None:
            text = ""
        elif isinstance(value, Enum):
            text = str(value.value)
        else:
            text = str(value)
        return ordinal, text

    return sorted(mentions, key=_key)


# --- Rule 7: the BLOB envelope (R18) --------------------------------------------

# The raw_json envelope key marking a binary cell (rule 7, R18 — exercised when the
# UFDR reader ships). "$"-prefixed so it can never collide with a real source column
# name that reaches raw_json verbatim.
BLOB_KEY = "$blob"


def blob_envelope(data: bytes) -> dict[str, dict[str, object]]:
    """The R18 BLOB envelope: ``{"$blob": {"sha256": …, "bytes": n}}`` (rule 7).

    HOW: sha256 of the cell's bytes (64-char lowercase hex) plus the byte count; the
    caller embeds the returned dict in the record's ``raw_json`` (serialised through
    ``canonical_json``, so the envelope's byte form is identical everywhere).
    WHY the bytes stay out of the run DB: duplicating binary payloads into every run
    would bloat a reproducible-scratch database for no evidential gain — the digest
    identifies the blob exactly, and the size lets an examiner reason about it; the
    payload remains addressable in the source artefact itself.
    """
    return {BLOB_KEY: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}
