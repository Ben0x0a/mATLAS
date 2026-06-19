"""SQLite extraction adapter for the source-agnostic pipeline."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from model_atlas.models import DiscoveredElement, ElementType
from model_atlas.presets.spec import PresetSpec
from model_atlas.sources.base import ExtractedData
from model_atlas.sources.registry import register_adapter
from model_atlas.sqlite.config import SourceHashMode
from model_atlas.sqlite.dedup import merge_with_source
from model_atlas.sqlite.extractor import extract_query, extract_table
from model_atlas.sqlite.integrity import verify_unchanged, verify_zip_fingerprint
from model_atlas.sqlite.locate import locate
from model_atlas.sqlite.sql_query import validate_custom_sql

log = logging.getLogger(__name__)


def _sidecar_label(wal_path: Path | None, journal_path: Path | None) -> str:
    if wal_path is not None:
        return "wal"
    if journal_path is not None:
        return "journal"
    return "sidecar"


def _db_relpath(element: DiscoveredElement, preset: PresetSpec) -> Path | None:
    config = preset.extract.get("sqlite", {}) if isinstance(preset.extract, dict) else {}
    if config.get("db_relpath"):
        log.debug("SQLite db_relpath from extract config: %s", config["db_relpath"])
        return Path(str(config["db_relpath"]))
    for selector in preset.selectors:
        if selector.get("source_type") == "sqlite" and selector.get("db_relpath"):
            log.debug("SQLite db_relpath from selector: %s", selector["db_relpath"])
            return Path(str(selector["db_relpath"]))
    if element.path.suffix.casefold() == ".zip" and element.source_original_path:
        log.debug("SQLite db_relpath from discovered ZIP element: %s", element.source_original_path)
        return Path(element.source_original_path)
    return None


def _content_fingerprint(located) -> str | None:
    """A stable content hash of the DB itself (not the wrapping archive), used to scope
    the generated record_uid. From the strategic ZIP fingerprint it is the extracted
    entry's decompressed content_sha256 (already computed); for a direct file it is the
    DB's full SHA-256. The DB entry is the one whose arcname is not a -wal/-shm/-journal
    sidecar."""
    fingerprint = located.source_fingerprint
    if fingerprint is not None:
        for entry in fingerprint.entries:
            if not entry.arcname.endswith(("-wal", "-shm", "-journal")):
                return entry.content_sha256
        return fingerprint.entries[0].content_sha256 if fingerprint.entries else None
    return located.source_hashes.get("db")


def _integrity_metadata(located, source_path: Path) -> dict:
    if located.source_fingerprint is not None:
        log.debug("Verifying SQLite ZIP strategic fingerprint: %s", source_path)
        report = verify_zip_fingerprint(located.source_fingerprint, source_path)
        return {
            "mode": SourceHashMode.STRATEGIC.value,
            "ok": report.ok,
            "source_fingerprint_before": located.source_fingerprint.to_dict(),
            "verification_after": report.to_dict(),
        }
    report = verify_unchanged(located.source_hashes, located.source_paths)
    log.debug("Verified SQLite source hashes: ok=%s paths=%s", report.ok, located.source_paths)
    return {
        "mode": SourceHashMode.FULL.value,
        "ok": report.ok,
        "source_hashes_before": located.source_hashes,
        "verification_after": report.to_dict(),
    }


def extract_sqlite(element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
    config = preset.extract.get("sqlite", {}) if isinstance(preset.extract, dict) else {}
    table = config.get("table")
    sql = config.get("sql")
    if bool(table) == bool(sql):
        raise ValueError(f"Preset {preset.name!r} must define exactly one sqlite table or sql")
    log.info("Reading SQLite source: %s", element.source_file)
    log.debug(
        "SQLite extraction config: element_path=%s source_original_path=%s table=%s sql_present=%s preset=%s",
        element.path,
        element.source_original_path,
        table,
        bool(sql),
        preset.name,
    )
    with tempfile.TemporaryDirectory() as td:
        located = locate(
            element.path,
            _db_relpath(element, preset),
            Path(td),
            SourceHashMode.STRATEGIC,
        )
        log.debug(
            "SQLite located files: tmp_db=%s wal=%s shm=%s journal=%s source_paths=%s",
            located.tmp_db_path,
            located.wal_path,
            located.shm_path,
            located.journal_path,
            located.source_paths,
        )
        if sql:
            validated_sql = validate_custom_sql(str(sql))
            log.debug("Executing validated SQLite custom SQL for preset %s", preset.name)
            db_only = extract_query(located.tmp_db_path, validated_sql, include_sidecars=False)
            with_sidecars = extract_query(
                located.tmp_db_path,
                validated_sql,
                include_sidecars=True,
                wal_path=located.wal_path,
                shm_path=located.shm_path,
                journal_path=located.journal_path,
            )
            source_file = f"{element.path.name}::query={preset.name}"
        else:
            table = str(table)
            log.debug("Extracting SQLite table %s for preset %s", table, preset.name)
            db_only = extract_table(located.tmp_db_path, table, include_sidecars=False)
            with_sidecars = extract_table(
                located.tmp_db_path,
                table,
                include_sidecars=True,
                wal_path=located.wal_path,
                shm_path=located.shm_path,
                journal_path=located.journal_path,
            )
            source_file = f"{element.path.name}::table={table}"

        source_columns = tuple(str(c) for c in with_sidecars.columns)
        merged = merge_with_source(
            db_only,
            with_sidecars,
            sidecar_label=_sidecar_label(located.wal_path, located.journal_path),
        )
        integrity = _integrity_metadata(located, element.path)
        log.debug(
            "SQLite extraction row counts: db_only=%d with_sidecars=%d merged=%d integrity_ok=%s",
            len(db_only),
            len(with_sidecars),
            len(merged),
            integrity.get("ok"),
        )

    source_original_path = str(_db_relpath(element, preset) or element.source_original_path)
    return ExtractedData(
        element=element,
        dataframe=merged,
        source_file=source_file,
        source_original_path=source_original_path,
        source_columns=source_columns,
        source_fingerprint=_content_fingerprint(located),
        metadata={
            "source_type": "sqlite",
            "path": str(element.path),
            "source_original_path": source_original_path,
            "source_file": source_file,
            "table": table,
            "custom_sql": str(sql) if sql else None,
            "row_count_db_only": len(db_only),
            "row_count_with_sidecars": len(with_sidecars),
            "row_count_merged": len(merged),
            "source_paths": {key: str(path) for key, path in located.source_paths.items()},
            "integrity": integrity,
        },
    )


@register_adapter
class SqliteAdapter:
    """Source adapter for SQLite databases (table or custom query)."""

    name = "sqlite"

    def can_handle(self, element: DiscoveredElement) -> bool:
        return element.source_type == ElementType.SQLITE

    def extract(self, element: DiscoveredElement, preset: PresetSpec) -> ExtractedData:
        return extract_sqlite(element, preset)
