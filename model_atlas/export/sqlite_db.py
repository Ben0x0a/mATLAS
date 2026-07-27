"""sqlite_db export brick: persist the run database itself via ``VACUUM INTO`` (T51 —
the new-engine replacement for the old flat-table ``sqlite`` export in
``exporters/builtin.py``).

Defines:    SqliteDbExporter (the ``Exporter`` protocol brick,
            ``format = "sqlite_db"``), registered in model_atlas.export.registry at
            import time.
Used by:    model_atlas.export.registry (registration) and
            tests/export/test_sqlite_db.py.
Depends on: model_atlas.core.store.RunStore (``.persist`` — VACUUM INTO),
            model_atlas.export.csv_flat (the ``Exporter`` protocol / ``ExportContext``).

Why this replaces, rather than ports, the old ``sqlite`` exporter
---------------------------------------------------------------------
The old exporter backed up the store's live connection wholesale too, but the OLD
store held a single denormalised ``assertions`` table plus an ``assertion_flat`` VIEW
— a flat-table dump. The relational store (T12/T13) already IS a full 21-table +
``matlas_meta`` schema with versioning pragmas stamped at creation
(``core.schema.create_schema``); there is no separate flat-table shape left to build,
so this brick is a thin call onto ``RunStore.persist`` (``VACUUM INTO``), producing a
clean, standalone, defragmented copy — every L0-L3 table, the two finding VIEWS, the
``application_id`` (R16 self-ingestion stamp) and ``user_version``. A caller who wants
the flat projection has ``CsvFlatExporter`` / ``core.flat_view.build_flat_view`` for that.

Encoding
--------
Binary SQLite file — no text encoding decision applies.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.core.store import RunStore
from model_atlas.export.csv_flat import ExportContext
from model_atlas.export.registry import register_exporter


class SqliteDbExporter:
    """Persists a clean, standalone copy of the run database (``format = "sqlite_db"``).

    Always writes ONE file for the whole run — like every exporter now (one file per
    format), every preset's data lives in the single persisted database.
    """

    format = "sqlite_db"

    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]:
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        path = ctx.output_dir / f"{ctx.base_name}.matlas.sqlite"
        store.persist(path)   # VACUUM INTO — see core.store.RunStore.persist
        return [path]


register_exporter(
    "sqlite_db", "Run database", SqliteDbExporter(),
    "The complete run database (relational model + matlas_meta), persisted via VACUUM INTO.",
)
