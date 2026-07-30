"""Merge SQLite db-only and db+sidecar views and annotate row origin.

Defines:    ``merge_with_source`` — combine the two reader passes (db-only, and
            db+WAL/journal sidecars) into one labelled frame; plus the two metadata
            colnames it appends.
Used by:    ``model_atlas.ingest.readers.sqlite_reader`` (the two-pass origin-labelling
            step, whose output it then splits into live vs recovered).
Depends on: ``pandas`` (the frame shape the reader hands it) and the stdlib only.

Row-origin labels:

- ``both``           : a committed db row seen in BOTH passes (the sidecar pass re-reads
                       every committed row, so a committed row appears twice — once per
                       pass — and must be counted ONCE, as live).
- ``wal`` / ``journal`` (``sidecar_label``) : a row present ONLY once the sidecar is
                       merged at read time — a genuine recovered row.
- ``db_only_unique`` : a row in db-only but absent from the sidecar pass (rare — e.g. an
                       uncommitted delete staged in the WAL).

Observation semantics (NO within-view dedup)
---------------------------------------------
Two GENUINELY DISTINCT rows that happen to share every column value are two independent
observations (two identical GPS pings, two identical log lines) and must BOTH survive as
records — never collapsed to one. Only the TRUE cross-pass overlap (the SAME committed
row re-read by the sidecar pass) may be coalesced.

We therefore match the two passes by VALUE MULTISET, not by set membership: each db-only
occurrence consumes at most ONE equal-valued sidecar occurrence, and any sidecar
occurrence left unconsumed is a real recovered row. Within a single pass every row is
emitted in order (its position is its identity), so duplicate-valued rows of the same
view are all preserved; across the two passes the match stays purely value-based, which
is exactly the WAL/journal merge. This replaces the earlier set-based scheme, which
silently dropped a second identical-valued row.

Dtype stability
---------------
A column that is all-integer in one pass but carries a NULL in the other is promoted
int64 -> float64 by pandas, which loses precision above 2**53 and would make a large
integer collide with its neighbour (or mis-match a committed row against its sidecar
re-read). ``_fingerprint_cell`` normalises every value to a dtype-stable canonical form
(an integral float and the equal int map to the same key; NULL/NaN map to one sentinel),
so the multiset match compares on VALUE, not on the incidental numpy dtype a NULL forced.
The reader additionally builds both frames with ``dtype=object`` so the promotion — and
its precision loss — never happens in the first place; this normalisation is the
value-level backstop for any caller that does not.
"""
from __future__ import annotations

import math
from collections import Counter

import pandas as pd

SOURCE_COLNAME = "_meta_sqlite_source"
SOURCE_ROW_NUMBER_COLNAME = "_meta_sqlite_source_row_number"

# Canonical stand-in for SQL NULL in a fingerprint. WHY a single sentinel: pandas
# represents NULL as either Python ``None`` or float ``NaN``, and ``NaN != NaN`` — without
# collapsing both to one value a NULL in one pass would never match a NULL in the other,
# so a committed row carrying a NULL would be mislabelled as sidecar-only (double-counted).
_NULL_FINGERPRINT = ("\0null\0",)


def _fingerprint_cell(value: object) -> object:
    """Normalise one cell to a dtype-stable, hashable fingerprint token.

    HOW: NULL (``None`` or ``NaN``) -> one shared sentinel; an integer, or a float whose
    value is integral, -> a tagged exact int (so ``123`` and ``123.0`` — the same value
    arriving under int64 in one pass and float64 in the other — match); a non-integral
    float -> a tagged float; ``bool`` kept distinct from int (SQLite has no bool, but a
    stray ``True`` must not merge with ``1``); everything else passes through unchanged.
    WHY tagged tuples: they keep an int token from ever hash-colliding with a look-alike
    string/float token, so the multiset match cannot conflate different types.
    """
    if value is None:
        return _NULL_FINGERPRINT
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, float):
        if math.isnan(value):
            return _NULL_FINGERPRINT
        if value.is_integer():
            return ("int", int(value))
        return ("float", value)
    if isinstance(value, int):
        return ("int", value)
    return value


def _row_fingerprints(df: pd.DataFrame) -> list[tuple]:
    """A dtype-stable, hashable fingerprint per row (a tuple of normalised cell tokens).

    The tuple keys the cross-pass multiset match downstream, so it must be hashable and
    compare equal for equal VALUES regardless of the numpy dtype a NULL happened to force
    on a column — see the module docstring.
    """
    records = df.to_dict(orient="records")
    columns = list(df.columns)
    return [tuple(_fingerprint_cell(row.get(column)) for column in columns) for row in records]


def merge_with_source(
    df_db_only: pd.DataFrame,
    df_with_sidecars: pd.DataFrame,
    sidecar_label: str = "wal",
) -> pd.DataFrame:
    """Merge the db-only and db+sidecars passes and label each output row by origin.

    Returns a DataFrame whose columns are the input columns **plus** two metadata columns:

    - ``_meta_sqlite_source`` (str): ``'both'``, ``'db_only_unique'``, or ``sidecar_label``
      (typically ``'wal'`` / ``'journal'``).
    - ``_meta_sqlite_source_row_number`` (int): the 1-based ordinal of the emitted row
      within the source view it came from. Not globally unique across the two views.

    Every db-only row is emitted (observation semantics — duplicate-valued rows are NOT
    coalesced); only sidecar occurrences with no matching db-only occurrence become
    recovered rows. See the module docstring for the multiset rationale.
    """
    if list(df_db_only.columns) != list(df_with_sidecars.columns):
        raise ValueError(
            "Column mismatch between db-only and db+sidecars extractions: "
            f"{list(df_db_only.columns)} vs {list(df_with_sidecars.columns)}"
        )

    fingerprints_db = _row_fingerprints(df_db_only)
    fingerprints_sidecars = _row_fingerprints(df_with_sidecars)
    db_rows = df_db_only.to_dict(orient="records")
    sidecar_rows = df_with_sidecars.to_dict(orient="records")

    # Per-value budget of sidecar occurrences still available to match a db-only row.
    # A committed row is present in BOTH passes; each db-only occurrence claims one equal
    # sidecar occurrence, so what remains after loop 1 is exactly the sidecar-only surplus.
    sidecar_available: Counter = Counter(fingerprints_sidecars)

    out_rows = []

    # Loop 1: EVERY db-only row is emitted (never skipped on a duplicate value). It is
    # 'both' when it can claim an equal-valued sidecar occurrence, else 'db_only_unique'.
    for row_number, (fingerprint, row) in enumerate(
        zip(fingerprints_db, db_rows), start=1
    ):
        if sidecar_available[fingerprint] > 0:
            sidecar_available[fingerprint] -= 1
            label = "both"
        else:
            label = "db_only_unique"
        # A fresh dict leaves the source row dict untouched; the metadata columns are
        # appended (private ``_meta_`` names cannot collide with a real column).
        out_rows.append({
            **row,
            SOURCE_COLNAME: label,
            SOURCE_ROW_NUMBER_COLNAME: row_number,
        })

    # Loop 2: emit only the sidecar occurrences NOT claimed above — the genuine recovered
    # rows. ``sidecar_available`` now holds, per fingerprint, how many occurrences are
    # surplus; we walk the sidecar rows in order and emit until each fingerprint's surplus
    # budget is spent. Because all occurrences of one fingerprint are value-identical,
    # which specific occurrence is emitted does not change the row's data (only its
    # source_row_number), so first-come emission is deterministic; duplicate WAL-only rows
    # are both emitted (their budget is 2+).
    surplus_remaining = dict(sidecar_available)
    for row_number, (fingerprint, row) in enumerate(
        zip(fingerprints_sidecars, sidecar_rows), start=1
    ):
        if surplus_remaining.get(fingerprint, 0) > 0:
            surplus_remaining[fingerprint] -= 1
            out_rows.append({
                **row,
                SOURCE_COLNAME: sidecar_label,
                SOURCE_ROW_NUMBER_COLNAME: row_number,
            })

    columns = list(df_db_only.columns) + [SOURCE_COLNAME, SOURCE_ROW_NUMBER_COLNAME]
    # dtype=object on the OUTPUT too: rebuilding a frame from ``out_rows`` would otherwise
    # RE-INFER dtypes and re-promote an integer column that now carries a NULL back to
    # float64 (NULL -> NaN, large ints -> lossy floats) — undoing the object-dtype the
    # caller passed in. Object dtype carries every value through as its native Python type.
    return pd.DataFrame(out_rows, columns=columns, dtype=object)
