"""Merge SQLite db-only and db+sidecar views and annotate row origin.

- 'both'           : row present in both views (committed db row)
- 'wal'            : row present only when the WAL is merged at read time
- 'db_only_unique' : row present in db-only but missing from db+sidecars (rare:
                     would happen if the WAL contains a delete not yet
                     committed, etc.)
"""
from __future__ import annotations

import pandas as pd

SOURCE_COLNAME = "_meta_sqlite_source"
SOURCE_ROW_NUMBER_COLNAME = "_meta_sqlite_source_row_number"


def _row_fingerprints(df: pd.DataFrame) -> list[tuple]:
    # Build a hashable fingerprint per row by projecting every column into a
    # tuple of Python scalars. The tuple is used as a dict/set key downstream,
    # so it must be hashable — that rules out raw DataFrame rows (Series) and
    # forces us through to_dict(orient="records") to get plain dicts of native
    # Python values, then tuple(...) to make them hashable.
    #
    # NaN/NULL normalisation: pandas represents SQL NULL as NaN, but
    # NaN != NaN, which would break equality checks across the two views. We
    # coerce NaN to None via df.where(pd.notna(df), None) so a NULL in one view
    # matches a NULL in the other.
    #
    # Forensic caveat: NULL == NULL under this scheme. Two distinct evidence
    # rows that happen to share all non-NULL columns and both have NULL in the
    # same positions are coalesced into a single output row. If your schema
    # has columns that legitimately distinguish rows only via NULL presence,
    # add a stable disambiguator (e.g. rowid) to the schema before extraction.
    records = df.where(pd.notna(df), None).to_dict(orient="records")
    return [tuple(r.get(c) for c in df.columns) for r in records]


def merge_with_source(
    df_db_only: pd.DataFrame,
    df_with_sidecars: pd.DataFrame,
    sidecar_label: str = "wal",
) -> pd.DataFrame:
    """Merge two extractions and label each output row by origin.

    Returns a DataFrame whose columns are the input columns **plus** two
    metadata columns:

    - ``_meta_sqlite_source`` (str): one of ``'both'``, ``'db_only_unique'``, or
      ``sidecar_label`` (typically ``'wal'`` or ``'journal'``).
    - ``_meta_sqlite_source_row_number`` (int): 1-based ordinal within the
      source view the row came from. Not globally unique across sources.

    Downstream source-agnostic transforms preserve these values in details;
    callers must not assume the returned DataFrame is column-compatible with
    either input on its own.
    """
    if list(df_db_only.columns) != list(df_with_sidecars.columns):
        raise ValueError(
            "Column mismatch between db-only and db+sidecars extractions: "
            f"{list(df_db_only.columns)} vs {list(df_with_sidecars.columns)}"
        )

    fingerprints_db = _row_fingerprints(df_db_only)
    fingerprints_sidecars = _row_fingerprints(df_with_sidecars)
    fingerprints_sidecars_set = set(fingerprints_sidecars)
    fingerprints_db_set = set(fingerprints_db)

    out_rows = []
    seen: set[tuple] = set()

    # 1. db-only rows first, labelled 'both' or 'db_only_unique'
    for row_number, (fingerprint, row) in enumerate(
        zip(fingerprints_db, df_db_only.to_dict(orient="records")),
        start=1,
    ):
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        label = "both" if fingerprint in fingerprints_sidecars_set else "db_only_unique"
        # {**row, ...} unpacks the row dict and appends/overrides the metadata
        # columns. Using a fresh dict keeps the original row dict untouched.
        out_rows.append({
            **row,
            SOURCE_COLNAME: label,
            SOURCE_ROW_NUMBER_COLNAME: row_number,
        })

    # 2. Sidecar-only rows next, labelled according to the sidecar type.
    for row_number, (fingerprint, row) in enumerate(
        zip(fingerprints_sidecars, df_with_sidecars.to_dict(orient="records")),
        start=1,
    ):
        if fingerprint in seen:
            continue
        # Invariant: by this point every fingerprint already in fingerprints_db
        # has been added to `seen` in loop 1, so it must have been caught by
        # the `in seen` branch above. Reaching here means the fingerprint is
        # genuinely sidecar-only.
        assert fingerprint not in fingerprints_db_set, (
            "dedup invariant violated: sidecar fingerprint also present in db_only "
            "but not caught by the 'seen' check"
        )
        seen.add(fingerprint)
        out_rows.append({
            **row,
            SOURCE_COLNAME: sidecar_label,
            SOURCE_ROW_NUMBER_COLNAME: row_number,
        })

    columns = list(df_db_only.columns) + [SOURCE_COLNAME, SOURCE_ROW_NUMBER_COLNAME]
    return pd.DataFrame(out_rows, columns=columns)
