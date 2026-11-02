"""The combined sheets' rows: one builder per sheet, each laid out by its static SheetDoc.

Defines:    Sheet, Context, the builders (source_device, communications, identifiers, locations)
            listed in SHEET_BUILDERS: the workbook's data sheets, fixed, in workbook order.
Used by:    workbook.brick (the orchestrator), which writes each Sheet.
Depends on: workbook.documentation (each sheet's layout and meaning), workbook.staging,
            workbook.links, workbook.comms, workbook.config, const (column names).

Each builder takes its header from its SheetDoc (documentation.expand_header) and yields one
dict per row keyed by header name; ``_align`` turns it into the header-ordered list the writer
streams. Record-based sheets put curated columns first (who, to whom, when), then every other
field of the record VERBATIM (so nothing the CSV holds is lost by the combination), then the
record id and the provenance columns. The only computed values are the joins and the To
inference, both described on the Documentation sheet.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ufdr_parser.bricks.workbook.comms import (
    comm_sources,
    TIME_ORDER_SQL,
    TIMESTAMP_FIELD_NAME,
)
from ufdr_parser.bricks.workbook.config import WorkbookConfig, load_workbook_config
from ufdr_parser.bricks.workbook.documentation import (
    ADDRESS_PREFIX,
    BLOCK_ADDRESS,
    BLOCK_COORDINATE,
    BLOCK_JOURNEY,
    BLOCK_OWN,
    COMMUNICATIONS,
    COORDINATE_PREFIX,
    IDENTIFIERS,
    JOURNEY_PREFIX,
    LOCATIONS,
    RECORD_UUID_COLNAME,
    SOURCE_DEVICE,
    TIMESTAMP_OFFSET_COLNAME,
    SheetDoc,
    expand_header,
)
from ufdr_parser.bricks.workbook.links import PARTY_TYPE_NAME, Links
from ufdr_parser.bricks.workbook.staging import Staging, Table, quote
from ufdr_parser.const import (
    COL_MAIN_UUID,
    COL_SOURCE_PATH,
    COL_SUB_UUID,
    COL_UUID,
    LEVEL_SUB,
    LEVEL_TOP,
    MULTI_VALUE_SEPARATOR,
    PARENT_FIELD_COLNAME,
    SOURCE_COLUMNS,
    TRACE_COLUMNS,
    level_file_prefix,
)
from ufdr_parser.report_info import (
    COL_DETAILS as REPORT_INFO_DETAILS_COLNAME,
    COL_NAME as REPORT_INFO_NAME_COLNAME,
    COL_SECTION as REPORT_INFO_SECTION_COLNAME,
    COL_VALUE as REPORT_INFO_VALUE_COLNAME,
    TOOL_CHECK_SECTION,
)

_ID_COLUMNS = (COL_UUID, COL_SUB_UUID, COL_MAIN_UUID)
_META_COLUMNS = frozenset(_ID_COLUMNS + SOURCE_COLUMNS + TRACE_COLUMNS)
_PROVENANCE_FIELDS = SOURCE_COLUMNS + TRACE_COLUMNS
_DEVICE_INFO_TYPE_NAME = "DeviceInfoEntry"


@dataclass
class Sheet:
    """One built sheet: its documentation, its actual header, the CSVs used, and its rows."""

    doc: SheetDoc
    header: list[str]  # internal column keys, the order rows are aligned to
    display: list[str]  # the names written on the sheet (tool columns prefixed)
    built_from: list[str]
    rows: Iterator[list[object]]


@dataclass
class Context:
    staging: Staging
    links: Links
    # (child_type, top_type) relations observed by the dump. mutable default — shared if = set()
    relations: set[tuple[str, str]] = field(default_factory=set)
    comm_files: dict[str, list[str]] = field(default_factory=dict)  # mutable default — per instance
    # The dump's report header CSV (see ufdr_parser.report_info); None if not written.
    report_info_path: Path | None = None
    # The active ufdr_config.toml (hidden columns, location types, identifier sources).
    config: WorkbookConfig = field(default_factory=lambda: load_workbook_config())


# --------------------------------------------------------------------------- shared helpers


def _own_columns(tables: list[Table], exclude: set[str]) -> list[str]:
    """The ordered union of the tables' own field columns, minus ids/provenance/``exclude``."""
    seen: dict[str, None] = {}
    for table in tables:
        for column in table.columns:
            if column not in _META_COLUMNS and column not in exclude:
                seen.setdefault(column, None)
    return list(seen)


def _align(header: list[str], values: dict[str, object]) -> list[object]:
    return [values.get(name) for name in header]


def _provenance(record_id: object, source: dict[str, object]) -> dict[str, object]:
    values = {name: source.get(name) for name in _PROVENANCE_FIELDS}
    values[RECORD_UUID_COLNAME] = record_id
    return values


def _own_values(raw: dict[str, object], names: list[str], renames: dict[str, str]) -> dict[str, object]:
    return {renames.get(name, name): raw.get(name) for name in names}


def _dict_rows(cursor: sqlite3.Cursor) -> Iterator[dict[str, object]]:
    names = [description[0] for description in cursor.description]
    for row in cursor:
        yield dict(zip(names, row))


def _raw_row(conn: sqlite3.Connection, table_name: str, seq: int) -> dict[str, object]:
    cursor = conn.execute(f"SELECT * FROM {table_name} WHERE seq = ?", (seq,))
    names = [description[0] for description in cursor.description]
    row = cursor.fetchone()
    return dict(zip(names, row)) if row else {}


# ------------------------------------------------------------------ communications

# Raw fields a curated column already shows (raw field, comm field), not repeated in the block.
_SHOWN_RAW = (
    (TIMESTAMP_FIELD_NAME, "timestamp"),
    (TIMESTAMP_OFFSET_COLNAME, "timestamp_offset"),
    ("Direction", "direction"),
    ("Status", "status"),
    ("Subject", "subject"),
    ("Body", "body"),
    ("Duration", "duration"),
    ("Source", "source"),
)


def _party_files(ctx: Context) -> list[str]:
    return [
        name
        for level, model_type in ctx.staging.groups()
        if model_type in (PARTY_TYPE_NAME, "Attachment") and (name := ctx.staging.csv_name(level, model_type))
    ]


def communications(ctx: Context) -> Sheet | None:
    if not ctx.comm_files:
        return None
    conn = ctx.staging.conn
    tables = []
    built_from: list[str] = []
    for source in comm_sources(ctx.staging):
        table = ctx.staging.table(source.level, source.model_type)
        if table is not None and table not in tables:
            tables.append(table)
    for files in ctx.comm_files.values():
        built_from += files
    fed = {column.field for column in COMMUNICATIONS.static() if column.field}
    own = _own_columns(tables, {raw for raw, comm in _SHOWN_RAW if comm in fed})
    header, display, renames = expand_header(COMMUNICATIONS, {BLOCK_OWN: own})
    fields = [(column.name, column.field) for column in COMMUNICATIONS.static() if column.field]

    def rows() -> Iterator[list[object]]:
        cursor = conn.cursor().execute(f"SELECT * FROM comm ORDER BY {TIME_ORDER_SQL}")
        for comm in _dict_rows(cursor):
            values = {name: comm[comm_field] for name, comm_field in fields}
            values.update(_provenance(comm["record_id"], comm))
            values.update(_own_values(_raw_row(conn, comm["raw_table"], comm["row_seq"]), own, renames))
            yield _align(header, values)

    files = list(dict.fromkeys(f for f in built_from + _party_files(ctx) if f))
    return Sheet(COMMUNICATIONS, header, display, files, rows())


# ------------------------------------------------------------------ identifiers

_BATCH = 5000


def identifiers(ctx: Context) -> Sheet | None:
    """Every identifier read from the configured sources, counted per (kind, source file, CSV).

    HOW: for each ``[[identifiers]]`` source, every dumped group of its model (whatever the
    nesting level) is read for the configured field (records failing ``only`` are skipped) and
    staged as (identifier, kind, source_path, CSV); the sheet is that table grouped and
    counted. Values are verbatim: no normalisation, so two spellings of a number stay two rows.
    """
    conn = ctx.staging.conn
    header, display, _renames = expand_header(IDENTIFIERS, {})
    conn.execute("CREATE TABLE ident (identifier TEXT, kind TEXT, source_file TEXT, csv TEXT)")
    used: list[str] = []
    for source in ctx.config.identifiers:
        for level, model_type in ctx.staging.groups():
            if model_type != source.model:
                continue
            table = ctx.staging.table(level, model_type)
            if table is None or not table.has(source.field):
                continue
            csv_name = ctx.staging.csv_name(level, model_type)
            used.append(csv_name)
            # A filter field the CSV lacks reads as NULL, so ``only`` then rejects the record.
            fields = list(dict.fromkeys([source.field, *source.only, *([source.kind_field] if source.kind_field else [])]))
            select = ", ".join(table.col(name) for name in fields + [COL_SOURCE_PATH])
            batch: list[tuple] = []
            cursor = conn.cursor().execute(
                f"SELECT {select} FROM {table.name} WHERE {quote(source.field)} IS NOT NULL ORDER BY seq"
            )
            for row in cursor:
                record = dict(zip(fields, row[:-1]))
                if source.accepts(record):
                    batch.append((record[source.field], source.kind_of(record), row[-1], csv_name))
                if len(batch) >= _BATCH:
                    conn.executemany("INSERT INTO ident VALUES (?, ?, ?, ?)", batch)
                    batch.clear()
            conn.executemany("INSERT INTO ident VALUES (?, ?, ?, ?)", batch)

    def rows() -> Iterator[list[object]]:
        cursor = conn.cursor().execute(
            "SELECT identifier, kind, source_file, csv, COUNT(*) FROM ident "
            "GROUP BY identifier, kind, source_file, csv ORDER BY identifier, kind, source_file, csv"
        )
        for identifier, kind, source_file, csv_name, count in cursor:
            yield _align(header, {
                "Identifier": identifier,
                "Kind": kind,
                "Source file": source_file,
                "CSV file": csv_name,
                "Occurrences": str(count),
            })

    return Sheet(IDENTIFIERS, header, display, list(dict.fromkeys(used)), rows())


# ------------------------------------------------------------------ locations


def _children(conn: sqlite3.Connection, table: Table, parent_id: object) -> list[dict[str, object]]:
    cursor = conn.execute(
        f"SELECT * FROM {table.name} WHERE {quote(COL_MAIN_UUID)} = ? ORDER BY seq", (parent_id,)
    )
    return list(_dict_rows(cursor))


_COORDINATE_TYPE_NAME = "Coordinate"
_ADDRESS_TYPE_NAME = "StreetAddress"
_LOCATION_TYPE_NAME = "Location"
_JOURNEY_TYPE_NAME = "Journey"
_JOURNEY_POINT_LABEL = "Journey point"
_LAT_LON = ("Latitude", "Longitude")
_RELATED_CONTAINER = "RelatedModels"


def _types_with_direct_coordinates(ctx: Context, coordinate: Table | None) -> list[str]:
    """Top-level types whose OWN records have a Coordinate child (a Position of their own).

    WHY not just the dump's relations: a relation is (child type, TOP type) at any depth, so a
    Chat whose messages hold a shared position would qualify and every chat would be listed as
    a position with blank coordinates. Only a direct (level-1) Coordinate child counts here.
    """
    if coordinate is None:
        return []
    found = []
    for top in sorted({top for child, top in ctx.relations if child == _COORDINATE_TYPE_NAME}):
        table = ctx.staging.table(LEVEL_TOP, top)
        if table is not None and ctx.staging.conn.execute(
            f"SELECT 1 FROM {coordinate.name} AS c JOIN {table.name} AS t "
            f"ON t.{quote(COL_UUID)} = c.{quote(COL_MAIN_UUID)} LIMIT 1"
        ).fetchone():
            found.append(top)
    return found


def locations(ctx: Context) -> Sheet | None:
    """Every position-bearing record at any depth, flattened verbatim, in report order (no sorting).

    The only additions are Record type and where Latitude/Longitude were copied from: the
    record's Coordinate child (one row per coordinate) or, failing that, a Latitude/Longitude
    field of the record itself. Everything else is the record's own fields, its Coordinate's
    and StreetAddress's fields, and for a journey point its journey's fields — all verbatim.
    A nested record's coordinate is one level below it, whatever the level.
    """
    conn = ctx.staging.conn
    level_of = {
        name: sorted(level for level, model in ctx.staging.groups() if model == name)
        for name in (_COORDINATE_TYPE_NAME, _ADDRESS_TYPE_NAME)
    }
    record_types = ctx.config.location_record_types
    detected = _types_with_direct_coordinates(ctx, ctx.staging.table(LEVEL_SUB, _COORDINATE_TYPE_NAME))
    # (model, level, table): the configured types at every level, others where they hold a Position.
    sources = [
        (model, level, table)
        for level, model in ctx.staging.groups()
        if model in record_types and (table := ctx.staging.table(level, model)) is not None
    ] + [
        (model, LEVEL_TOP, ctx.staging.table(LEVEL_TOP, model))
        for model in detected
        if model not in record_types
    ]
    if not sources:
        return None
    sources.sort(key=lambda item: (list(record_types).index(item[0]) if item[0] in record_types else len(record_types), item[1]))
    journey = ctx.staging.table(LEVEL_TOP, _JOURNEY_TYPE_NAME) if _JOURNEY_TYPE_NAME in record_types else None
    points = ctx.staging.table(LEVEL_SUB, _LOCATION_TYPE_NAME) if journey is not None else None
    journey_ids = (
        {row[0] for row in conn.execute(f"SELECT {quote(COL_UUID)} FROM {journey.name}")} if journey is not None else set()
    )

    def tables_of(name: str) -> list[Table]:
        return [ctx.staging.table(level, name) for level in level_of[name]]

    coordinate_fields = [c for c in _own_columns(tables_of(_COORDINATE_TYPE_NAME), set()) if c not in _LAT_LON]
    address_fields = _own_columns(tables_of(_ADDRESS_TYPE_NAME), set())
    journey_fields = _own_columns([journey], set()) if journey is not None else []
    own = _own_columns(
        [table for model, level, table in sources if not (model == _JOURNEY_TYPE_NAME and level == LEVEL_TOP)], set()
    )
    header, display, renames = expand_header(
        LOCATIONS,
        {
            BLOCK_COORDINATE: [f"{COORDINATE_PREFIX}{c}" for c in coordinate_fields],
            BLOCK_ADDRESS: [f"{ADDRESS_PREFIX}{c}" for c in address_fields],
            BLOCK_JOURNEY: [f"{JOURNEY_PREFIX}{c}" for c in journey_fields],
            BLOCK_OWN: own,
        },
    )

    def is_point(record: dict) -> bool:
        """A journey's way point, listed under its journey; a RELATED location is not one."""
        return record.get(COL_MAIN_UUID) in journey_ids and record.get(PARENT_FIELD_COLNAME) != _RELATED_CONTAINER

    def build(record_type: str, record: dict, record_id: object, child_level: int, extra: dict[str, object],
              *, require_coordinate: bool = False) -> Iterator[list[object]]:
        values = _own_values(record, own, renames)
        values.update(extra)
        values["Record type"] = record_type
        address_table = ctx.staging.table(child_level, _ADDRESS_TYPE_NAME)
        if address_table is not None:
            found_addresses = _children(conn, address_table, record_id)
            for name in address_fields:
                joined = MULTI_VALUE_SEPARATOR.join(str(a[name]) for a in found_addresses if a.get(name))
                values[f"{ADDRESS_PREFIX}{name}"] = joined or None
        values.update(_provenance(record_id, record))
        coord_table = ctx.staging.table(child_level, _COORDINATE_TYPE_NAME)
        found = _children(conn, coord_table, record_id) if coord_table is not None else []
        if require_coordinate and not found:
            return
        if not found:
            # No Coordinate child: a Latitude/Longitude field of the record itself, if any.
            row = dict(values)
            for name in _LAT_LON:
                if record.get(name):
                    row[name], row[f"{name} field"] = record[name], name
            yield _align(header, row)
            return
        # Named after the raw CSV it comes from (SMCoordinate / SSMCoordinate), as the dump names it.
        label = f"{level_file_prefix(child_level)}{_COORDINATE_TYPE_NAME}"
        # One row per coordinate (a Position is 1:1 in practice).
        for coord in found:
            row = dict(values)
            for name in _LAT_LON:
                if coord.get(name):
                    row[name], row[f"{name} field"] = coord[name], f"{label}.{name}"
            row.update({f"{COORDINATE_PREFIX}{c}": coord.get(c) for c in coordinate_fields})
            yield _align(header, row)

    def rows() -> Iterator[list[object]]:
        for model_type, level, table in sources:
            id_column = COL_UUID if level == LEVEL_TOP else COL_SUB_UUID
            for record in _dict_rows(conn.cursor().execute(f"SELECT * FROM {table.name} ORDER BY seq")):
                record_id = record[id_column]
                if model_type == _JOURNEY_TYPE_NAME and level == LEVEL_TOP:
                    journey_values = {f"{JOURNEY_PREFIX}{c}": record.get(c) for c in journey_fields}
                    journey_points = [
                        point for point in (_children(conn, points, record_id) if points is not None else [])
                        if is_point(point)
                    ]
                    if not journey_points:
                        yield from build(model_type, record, record_id, level + 1, journey_values)
                    for point in journey_points:
                        yield from build(_JOURNEY_POINT_LABEL, point, point[COL_SUB_UUID], level + 2, journey_values)
                    continue
                if model_type == _LOCATION_TYPE_NAME and level == LEVEL_SUB and is_point(record):
                    continue  # listed above, under its journey
                # A type listed only because some of its records carry a coordinate (e.g.
                # SocialMediaActivity) contributes just those records; configured types are
                # listed whole.
                yield from build(
                    model_type, record, record_id, level + 1, {}, require_coordinate=model_type not in record_types
                )

    files = [ctx.staging.csv_name(level, model) for model, level, _table in sources] + [
        ctx.staging.csv_name(level, name) for name, levels in level_of.items() for level in levels
    ]
    return Sheet(LOCATIONS, header, display, list(dict.fromkeys(f for f in files if f)), rows())


# ------------------------------------------------------------------ source device


def source_device(ctx: Context) -> Sheet | None:
    """The report header values (report_info CSV), then the decoded DeviceInfoEntry records."""
    conn = ctx.staging.conn
    device = ctx.staging.table(LEVEL_TOP, _DEVICE_INFO_TYPE_NAME)
    info_path = ctx.report_info_path
    if device is None and (info_path is None or not info_path.is_file()):
        return None
    header, display, _renames = expand_header(SOURCE_DEVICE, {})

    def rows() -> Iterator[list[object]]:
        if info_path is not None and info_path.is_file():
            with info_path.open(encoding="utf-8-sig", newline="") as fh:
                for record in csv.DictReader(fh):
                    if record[REPORT_INFO_SECTION_COLNAME] == TOOL_CHECK_SECTION:
                        continue
                    values = {
                        "Section": record[REPORT_INFO_SECTION_COLNAME],
                        "Name": record[REPORT_INFO_NAME_COLNAME],
                        "Value": record[REPORT_INFO_VALUE_COLNAME] or None,
                        "Details": record[REPORT_INFO_DETAILS_COLNAME] or None,
                    }
                    values.update(_provenance(None, record))
                    yield _align(header, values)
        if device is None:
            return
        for record in _dict_rows(conn.cursor().execute(f"SELECT * FROM {device.name} ORDER BY seq")):
            values = {
                "Section": f"Device info: {record.get('EntryCategory') or '(no category)'}",
                "Name": record.get("EntryName"),
                "Value": record.get("EntryValue"),
                "Details": f"Source={record['Source']}" if record.get("Source") else None,
                TIMESTAMP_FIELD_NAME: record.get(TIMESTAMP_FIELD_NAME),
                TIMESTAMP_OFFSET_COLNAME: record.get(TIMESTAMP_OFFSET_COLNAME),
            }
            values.update(_provenance(record[COL_UUID], record))
            yield _align(header, values)

    files = [info_path.name if info_path is not None else None, ctx.staging.csv_name(LEVEL_TOP, _DEVICE_INFO_TYPE_NAME)]
    return Sheet(SOURCE_DEVICE, header, display, [f for f in files if f], rows())


# Workbook order, matching documentation.SHEET_DOCS (About and Documentation come first).
SHEET_BUILDERS: dict[str, Callable[[Context], Sheet | None]] = {
    SOURCE_DEVICE.name: source_device,
    COMMUNICATIONS.name: communications,
    IDENTIFIERS.name: identifiers,
    LOCATIONS.name: locations,
}
