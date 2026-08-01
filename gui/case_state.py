"""Case-context view helpers for the GUI: read the running totals of a persisted case
database and format them for the window title / status line.

Defines:    CaseCounts (runs / sources / assertions), read_case_counts (query a persisted
            case DB read-only) and format_case_summary (the one-line human summary).
Used by:    gui.app (MainController surfaces the active case after each run).
Depends on: the standard library ONLY (sqlite3) — deliberately Qt-free, so the counts +
            formatting are unit-tested without building a window (MVC: this view-model
            logic lives outside the controller, which only displays the string).

The database is opened READ-ONLY (``mode=ro``), mirroring
``gui.spatiotemporal_controller._PersistedRunView``: surfacing a summary must never be able
to mutate the evidence database. A direct sqlite3 open (rather than importing that view) is
what keeps this module import-light and Qt-free.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CaseCounts:
    """The running totals a case has accumulated across every run appended to it."""

    runs: int
    sources: int
    assertions: int


def read_case_counts(db_path: str | Path) -> CaseCounts:
    """Read the case's run / source / assertion totals from its persisted database.

    Opened READ-ONLY so surfacing the summary can never mutate the evidence database. The
    caller guards failures — a summary is best-effort feedback, never part of the run's
    correctness."""
    # URI mode=ro: the file must exist AND the connection physically cannot write. The path is
    # percent-encoded via Path.as_uri() (which yields an absolute file: URI) BEFORE the query
    # string is appended, so a folder carrying a URI-significant character (#, ?, %) — e.g.
    # "Case #3" — is not mis-parsed by SQLite's URI parser into a bogus path + fragment.
    connection = sqlite3.connect(f"{Path(db_path).absolute().as_uri()}?mode=ro", uri=True)
    try:
        runs = connection.execute("SELECT COUNT(*) FROM run").fetchone()[0]
        sources = connection.execute("SELECT COUNT(*) FROM source").fetchone()[0]
        assertions = connection.execute("SELECT COUNT(*) FROM assertion").fetchone()[0]
        return CaseCounts(runs=runs, sources=sources, assertions=assertions)
    finally:
        connection.close()


def format_case_summary(case_name: str, counts: CaseCounts) -> str:
    """The one-line case summary shown after a run, e.g.
    ``Case 'op': 2 run(s) · 3 source(s) · 512 assertion(s)``."""
    return (f"Case {case_name!r}: {counts.runs} run(s) · {counts.sources} source(s) · "
            f"{counts.assertions} assertion(s)")
