"""The combined sheets' rows: one builder per sheet, each laid out by its static SheetDoc.

Defines:    Sheet, Context, the builders (communications, messages, calls, emails, contacts,
            locations, identifiers, cdr_importer) listed in SHEET_BUILDERS in workbook order,
            classify_identifier and describe_child.
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
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from ufdr_parser.bricks.workbook.comms import (
    CATEGORY_CALL,
    CATEGORY_CHAT_MESSAGE,
    CATEGORY_EMAIL,
    CATEGORY_INSTANT_MESSAGE,
    CHAT_TYPE_NAME,
    COMM_SOURCES,
    TIME_ORDER_SQL,
    TIMESTAMP_FIELD_NAME,
)
from ufdr_parser.bricks.workbook.config import (
    CONTACT_APP_COLUMNS,
    CONTACT_EMAIL_COLNAME,
    CONTACT_OTHER_IDS_COLNAME,
    CONTACT_PHONE_COLNAME,
    LOCATION_RECORD_TYPES,
)
from ufdr_parser.bricks.workbook.documentation import (
    ADDRESS_PREFIX,
    BLOCK_ADDRESS,
    BLOCK_CHILDREN,
    BLOCK_COORDINATE,
    BLOCK_JOURNEY,
    BLOCK_OWN,
    CALLS,
    CDR_IMPORTER,
    COMMUNICATIONS,
    CONTACT_CHILD_COLUMNS,
    CONTACTS,
    COORDINATE_PREFIX,
    EMAILS,
    IDENTIFIERS,
    JOURNEY_PREFIX,
    LOCATIONS,
    MESSAGES,
    RECORD_UUID_COLNAME,
    SOURCE_DEVICE,
    TIMESTAMP_OFFSET_COLNAME,
    SheetDoc,
    expand_header,
)
from ufdr_parser.bricks.workbook.links import PARTY_TYPE_NAME, ROLE_FROM, ROLE_TO, Links
from ufdr_parser.bricks.workbook.staging import Staging, Table, quote
from ufdr_parser.const import (
    COL_MAIN_UUID,
    COL_SUB_UUID,
    COL_UUID,
    LEVEL_FILE_PREFIX,
    LEVEL_SUB,
    LEVEL_TOP,
    MULTI_VALUE_SEPARATOR,
    SOURCE_COLUMNS,
    TRACE_COLUMNS,
)
from ufdr_parser.report_info import (
    COL_DETAILS as REPORT_INFO_DETAILS_COLNAME,
    COL_NAME as REPORT_INFO_NAME_COLNAME,
    COL_SECTION as REPORT_INFO_SECTION_COLNAME,
    COL_VALUE as REPORT_INFO_VALUE_COLNAME,
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


# ------------------------------------------------------------------ communication sheets

_TIME_ORDER = TIME_ORDER_SQL
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
        for level in (LEVEL_SUB, LEVEL_SUB + 1)
        for model_type in (PARTY_TYPE_NAME, "Attachment")
        if (name := ctx.staging.csv_name(level, model_type))
    ]


def _comm_sheet(ctx: Context, doc: SheetDoc, categories: tuple[str, ...], order_sql: str) -> Sheet | None:
    conn = ctx.staging.conn
    present = [category for category in categories if category in ctx.comm_files]
    if not present:
        return None
    tables = []
    built_from: list[str] = []
    for source in COMM_SOURCES:
        if source.category in present:
            table = ctx.staging.table(source.level, source.model_type)
            if table is not None:
                tables.append(table)
            built_from += ctx.comm_files[source.category]
    fed = {column.field for column in doc.static() if column.field}
    own = _own_columns(tables, {raw for raw, comm in _SHOWN_RAW if comm in fed})
    header, display, renames = expand_header(doc, {BLOCK_OWN: own})
    has_own_block = any(column.block == BLOCK_OWN for column in doc.columns)
    fields = [(column.name, column.field) for column in doc.static() if column.field]
    placeholders = ", ".join("?" for _ in present)

    def rows() -> Iterator[list[object]]:
        # The per-chat window (Messages' grouping) sorts a copy of every row, so it is only
        # computed for the sheet that orders by it; the others read the time index directly.
        window = ", MIN(seq) OVER (PARTITION BY chat_id) AS chat_order" if "chat_order" in order_sql else ""
        cursor = conn.cursor().execute(
            f"SELECT *{window} FROM comm WHERE category IN ({placeholders}) ORDER BY {order_sql}",
            present,
        )
        for comm in _dict_rows(cursor):
            values = {name: comm[comm_field] for name, comm_field in fields}
            values.update(_provenance(comm["record_id"], comm))
            if has_own_block:
                values.update(_own_values(_raw_row(conn, comm["raw_table"], comm["row_seq"]), own, renames))
            yield _align(header, values)

    files = list(dict.fromkeys(f for f in built_from + _party_files(ctx) if f))
    return Sheet(doc, header, display, files, rows())


def communications(ctx: Context) -> Sheet | None:
    return _comm_sheet(
        ctx,
        COMMUNICATIONS,
        (CATEGORY_CALL, CATEGORY_EMAIL, CATEGORY_INSTANT_MESSAGE, CATEGORY_CHAT_MESSAGE),
        _TIME_ORDER,
    )


def messages(ctx: Context) -> Sheet | None:
    return _comm_sheet(
        ctx,
        MESSAGES,
        (CATEGORY_CHAT_MESSAGE, CATEGORY_INSTANT_MESSAGE),
        f"category <> '{CATEGORY_CHAT_MESSAGE}', chat_order, {_TIME_ORDER}",
    )


def calls(ctx: Context) -> Sheet | None:
    return _comm_sheet(ctx, CALLS, (CATEGORY_CALL,), _TIME_ORDER)


def emails(ctx: Context) -> Sheet | None:
    return _comm_sheet(ctx, EMAILS, (CATEGORY_EMAIL,), _TIME_ORDER)


# ------------------------------------------------------------------ CDR importer (legacy)


def cdr_importer(ctx: Context) -> Sheet | None:
    if not ctx.comm_files:
        return None
    conn = ctx.staging.conn
    header, display, _renames = expand_header(CDR_IMPORTER, {})
    fields = [(column.name, column.field) for column in CDR_IMPORTER.static() if column.field]

    def rows() -> Iterator[list[object]]:
        cursor = conn.cursor().execute(
            f"SELECT * FROM comm ORDER BY {TIME_ORDER_SQL}"
        )
        for comm in _dict_rows(cursor):
            values = {name: comm[comm_field] for name, comm_field in fields}
            values["B_MSISDN"] = comm["cdr_b"]
            values["B_basis"] = comm["cdr_b_basis"]
            values["Comment"] = comm["body"] or comm["subject"]
            values.update(_provenance(comm["record_id"], comm))
            yield _align(header, values)

    files = list(dict.fromkeys(f for files in ctx.comm_files.values() for f in files))
    return Sheet(CDR_IMPORTER, header, display, files + _party_files(ctx), rows())


# ------------------------------------------------------------------ identifiers (legacy)


def identifiers(ctx: Context) -> Sheet | None:
    conn = ctx.staging.conn
    if not conn.execute("SELECT 1 FROM party LIMIT 1").fetchone():
        return None
    # Kind of record each party hangs off: a communication (from comm), a chat (participant),
    # or any other top-level type the dump saw with Party children (Notification, ...).
    conn.execute("CREATE TABLE parent_kind (id TEXT PRIMARY KEY, kind TEXT, app TEXT, ts TEXT)")
    conn.execute(
        "INSERT OR IGNORE INTO parent_kind SELECT record_id, category, source, timestamp FROM comm"
    )
    other_types = sorted({top for child, top in ctx.relations if child == PARTY_TYPE_NAME})
    for model_type in [CHAT_TYPE_NAME] + [t for t in other_types if t != CHAT_TYPE_NAME]:
        table = ctx.staging.table(LEVEL_TOP, model_type)
        if table is None:
            continue
        kind = "Chat (participant)" if model_type == CHAT_TYPE_NAME else model_type
        conn.execute(
            f"INSERT OR IGNORE INTO parent_kind SELECT {quote(COL_UUID)}, ?, "
            f"{table.col('Source')}, {table.col(TIMESTAMP_FIELD_NAME)} FROM {table.name}",
            (kind,),
        )
    header, display, _renames = expand_header(IDENTIFIERS, {})

    def rows() -> Iterator[list[object]]:
        cursor = conn.cursor().execute(
            "SELECT p.c0, p.c1, p.c2, p.c3, k.kind, k.app, k.ts FROM party AS p "
            "LEFT JOIN parent_kind AS k ON k.id = p.parent_id "
            "WHERE p.c0 IS NOT NULL ORDER BY p.c0, p.seq"
        )
        current: str | None = None
        acc: dict = {}
        for ident, name, role, owner, kind, app, ts in cursor:
            if ident != current:
                if current is not None:
                    yield _align(header, _identifier_row(current, acc))
                current, acc = ident, {"names": {}, "owner": False, "n": 0, "roles": {}, "kinds": {}, "apps": {}, "ts": []}
            acc["n"] += 1
            if name:
                acc["names"].setdefault(name, None)
            acc["owner"] |= (owner or "").lower() == "true"
            acc["roles"][role or "no role"] = acc["roles"].get(role or "no role", 0) + 1
            acc["kinds"][kind or "other"] = acc["kinds"].get(kind or "other", 0) + 1
            if app:
                acc["apps"].setdefault(app, None)
            if ts:
                acc["ts"].append(ts)
        if current is not None:
            yield _align(header, _identifier_row(current, acc))

    return Sheet(IDENTIFIERS, header, display, _party_files(ctx), rows())


def _counts(counter: dict[str, int]) -> str | None:
    return MULTI_VALUE_SEPARATOR.join(f"{key} x{value}" for key, value in sorted(counter.items())) or None


def _identifier_row(identifier: str, acc: dict) -> dict[str, object]:
    roles = acc["roles"]
    others = {role: count for role, count in roles.items() if role not in (ROLE_FROM, ROLE_TO)}
    return {
        "Identifier": identifier,
        "Names": MULTI_VALUE_SEPARATOR.join(acc["names"]) or None,
        "Flagged as phone owner": "yes" if acc["owner"] else None,
        "Occurrences": str(acc["n"]),
        "As sender": str(roles.get(ROLE_FROM, 0)),
        "As recipient": str(roles.get(ROLE_TO, 0)),
        "Other roles": _counts(others),
        "Seen in": _counts(acc["kinds"]),
        "Sources": MULTI_VALUE_SEPARATOR.join(acc["apps"]) or None,
        "First seen": min(acc["ts"]) if acc["ts"] else None,
        "Last seen": max(acc["ts"]) if acc["ts"] else None,
    }


# ------------------------------------------------------------------ contacts & locations

_ADDRESS_PARTS = ("Street1", "Street2", "HouseNumber", "PostalCode", "City", "State", "Country", "Neighborhood")


def describe_child(model_type: str, row: dict[str, object]) -> str | None:
    """One child model as text: ``Category: Value``, an address line, ``Key: Value``..."""
    if model_type == "StreetAddress":
        parts = [str(row[p]) for p in _ADDRESS_PARTS if row.get(p)]
        return ", ".join(parts) or None
    if model_type == "KeyValueModel" and row.get("Key"):
        return f"{row['Key']}: {row.get('Value') or ''}"
    if row.get("Value"):
        return f"{row['Category']}: {row['Value']}" if row.get("Category") else str(row["Value"])
    pairs = [
        f"{key}={value}"
        for key, value in row.items()
        if value and key not in _META_COLUMNS and key != "seq"
    ]
    return ", ".join(pairs) or None


def _children(conn: sqlite3.Connection, table: Table, parent_id: object) -> list[dict[str, object]]:
    cursor = conn.execute(
        f"SELECT * FROM {table.name} WHERE {quote(COL_MAIN_UUID)} = ? ORDER BY seq", (parent_id,)
    )
    return list(_dict_rows(cursor))


def _joined_children(conn: sqlite3.Connection, model_type: str, table: Table | None, parent_id: object) -> str | None:
    if table is None:
        return None
    texts = [t for t in (describe_child(model_type, r) for r in _children(conn, table, parent_id)) if t]
    return MULTI_VALUE_SEPARATOR.join(texts) or None


# Contact child types that carry identifiers, spread over the identifier columns.
_PHONE_TYPE_NAME = "PhoneNumber"
_EMAIL_TYPE_NAME = "EmailAddress"
_USER_ID_TYPE_NAME = "UserID"
_IDENTIFIER_TYPES = (_PHONE_TYPE_NAME, _EMAIL_TYPE_NAME, _USER_ID_TYPE_NAME)
_APP_PATTERNS = tuple(
    (header, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)) for header, pattern in CONTACT_APP_COLUMNS
)


def classify_identifier(category: object, contact_source: object) -> str:
    """The Contacts column a UserID entry belongs to (see workbook.config).

    HOW: the first application whose pattern matches the entry's Category wins; failing that,
    the first matching the contact's Source; otherwise the Other identifiers column. WHY the
    Category first: a contact merged from several apps can hold e.g. an "Instagram Id" under a
    Source that names another app, and the entry's own label is the more specific statement.
    """
    for text in (category, contact_source):
        if not text:
            continue
        for header, pattern in _APP_PATTERNS:
            if pattern.search(str(text)):
                return header
    return CONTACT_OTHER_IDS_COLNAME


def contacts(ctx: Context) -> Sheet | None:
    conn = ctx.staging.conn
    contact = ctx.staging.table(LEVEL_TOP, "Contact")
    if contact is None:
        return None
    id_tables = {t: ctx.staging.table(LEVEL_SUB, t) for t in _IDENTIFIER_TYPES}
    fixed = {t: ctx.staging.table(LEVEL_SUB, t) for t, _header in CONTACT_CHILD_COLUMNS}
    fixed_headers = dict(CONTACT_CHILD_COLUMNS)
    # Any other child type the dump saw under Contact gets a column named after it.
    extra = [
        (child, table)
        for child, top in sorted(ctx.relations)
        if top == "Contact"
        and child not in _IDENTIFIER_TYPES
        and child not in fixed_headers
        and (table := ctx.staging.table(LEVEL_SUB, child)) is not None
    ]
    own = _own_columns([contact], set())
    header, display, renames = expand_header(CONTACTS, {BLOCK_OWN: own, BLOCK_CHILDREN: [c for c, _t in extra]})
    id_headers = [CONTACT_PHONE_COLNAME, CONTACT_EMAIL_COLNAME] + [h for h, _p in CONTACT_APP_COLUMNS] + [
        CONTACT_OTHER_IDS_COLNAME
    ]

    def rows() -> Iterator[list[object]]:
        for record in _dict_rows(conn.cursor().execute(f"SELECT * FROM {contact.name} ORDER BY seq")):
            parent = record[COL_UUID]
            buckets: dict[str, list[str]] = {name: [] for name in id_headers}
            for model_type, table in id_tables.items():
                if table is None:
                    continue
                for child in _children(conn, table, parent):
                    text = describe_child(model_type, child)
                    if not text:
                        continue
                    if model_type == _PHONE_TYPE_NAME:
                        column = CONTACT_PHONE_COLNAME
                    elif model_type == _EMAIL_TYPE_NAME:
                        column = CONTACT_EMAIL_COLNAME
                    else:
                        column = classify_identifier(child.get("Category"), record.get("Source"))
                    buckets[column].append(text)
            values = _own_values(record, own, renames)
            values.update({name: MULTI_VALUE_SEPARATOR.join(texts) or None for name, texts in buckets.items()})
            for model_type, table in fixed.items():
                values[fixed_headers[model_type]] = _joined_children(conn, model_type, table, parent)
            for model_type, table in extra:
                values[renames.get(model_type, model_type)] = _joined_children(conn, model_type, table, parent)
            values.update(_provenance(parent, record))
            yield _align(header, values)

    files = [ctx.staging.csv_name(LEVEL_TOP, "Contact")] + [
        ctx.staging.csv_name(LEVEL_SUB, t) for t in list(id_tables) + list(fixed) + [c for c, _t in extra]
    ]
    return Sheet(CONTACTS, header, display, [f for f in files if f], rows())


_COORDINATE_TYPE_NAME = "Coordinate"
_ADDRESS_TYPE_NAME = "StreetAddress"
_LOCATION_TYPE_NAME = "Location"
_JOURNEY_TYPE_NAME = "Journey"
_JOURNEY_POINT_LABEL = "Journey point"
_LAT_LON = ("Latitude", "Longitude")


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
    """Every position-bearing record, flattened verbatim, in report order (no sorting).

    The only additions are Record type and where Latitude/Longitude were copied from: the
    record's Coordinate child (one row per coordinate) or, failing that, a Latitude/Longitude
    field of the record itself. Everything else is the record's own fields, its Coordinate's
    and StreetAddress's fields, and for a journey point its journey's fields — all verbatim.
    """
    conn = ctx.staging.conn
    coords = {level: ctx.staging.table(level, _COORDINATE_TYPE_NAME) for level in (LEVEL_SUB, LEVEL_SUB + 1)}
    addresses = {level: ctx.staging.table(level, _ADDRESS_TYPE_NAME) for level in (LEVEL_SUB, LEVEL_SUB + 1)}
    types = list(LOCATION_RECORD_TYPES) + [
        t for t in _types_with_direct_coordinates(ctx, coords[LEVEL_SUB]) if t not in LOCATION_RECORD_TYPES
    ]
    tables = {t: table for t in types if (table := ctx.staging.table(LEVEL_TOP, t)) is not None}
    if not tables:
        return None
    journey = tables.get(_JOURNEY_TYPE_NAME)
    points = ctx.staging.table(LEVEL_SUB, _LOCATION_TYPE_NAME) if journey is not None else None

    coordinate_fields = [c for c in _own_columns([t for t in coords.values() if t], set()) if c not in _LAT_LON]
    address_fields = _own_columns([t for t in addresses.values() if t], set())
    journey_fields = _own_columns([journey], set()) if journey is not None else []
    own_tables = [t for name, t in tables.items() if name != _JOURNEY_TYPE_NAME] + ([points] if points else [])
    own = _own_columns(own_tables, set())
    header, display, renames = expand_header(
        LOCATIONS,
        {
            BLOCK_COORDINATE: [f"{COORDINATE_PREFIX}{c}" for c in coordinate_fields],
            BLOCK_ADDRESS: [f"{ADDRESS_PREFIX}{c}" for c in address_fields],
            BLOCK_JOURNEY: [f"{JOURNEY_PREFIX}{c}" for c in journey_fields],
            BLOCK_OWN: own,
        },
    )

    def build(record_type: str, record: dict, record_id: object, level: int, extra: dict[str, object],
              *, require_coordinate: bool = False) -> Iterator[list[object]]:
        values = _own_values(record, own, renames)
        values.update(extra)
        values["Record type"] = record_type
        address_table = addresses[level]
        if address_table is not None:
            found_addresses = _children(conn, address_table, record_id)
            for name in address_fields:
                joined = MULTI_VALUE_SEPARATOR.join(str(a[name]) for a in found_addresses if a.get(name))
                values[f"{ADDRESS_PREFIX}{name}"] = joined or None
        values.update(_provenance(record_id, record))
        coord_table = coords[level]
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
        label = f"{LEVEL_FILE_PREFIX[level]}{_COORDINATE_TYPE_NAME}"
        # One row per coordinate (a Position is 1:1 in practice).
        for coord in found:
            row = dict(values)
            for name in _LAT_LON:
                if coord.get(name):
                    row[name], row[f"{name} field"] = coord[name], f"{label}.{name}"
            row.update({f"{COORDINATE_PREFIX}{c}": coord.get(c) for c in coordinate_fields})
            yield _align(header, row)

    def rows() -> Iterator[list[object]]:
        for model_type, table in tables.items():
            for record in _dict_rows(conn.cursor().execute(f"SELECT * FROM {table.name} ORDER BY seq")):
                record_id = record[COL_UUID]
                if model_type != _JOURNEY_TYPE_NAME:
                    # A type listed only because some of its records carry a coordinate (e.g.
                    # SocialMediaActivity) contributes just those records; configured types are
                    # listed whole.
                    yield from build(model_type, record, record_id, LEVEL_SUB, {},
                                     require_coordinate=model_type not in LOCATION_RECORD_TYPES)
                    continue
                journey_values = {f"{JOURNEY_PREFIX}{c}": record.get(c) for c in journey_fields}
                journey_points = _children(conn, points, record_id) if points is not None else []
                if not journey_points:
                    yield from build(model_type, record, record_id, LEVEL_SUB, journey_values)
                    continue
                for point in journey_points:
                    yield from build(_JOURNEY_POINT_LABEL, point, point[COL_SUB_UUID], LEVEL_SUB + 1, journey_values)

    files = [ctx.staging.csv_name(LEVEL_TOP, t) for t in tables] + [
        ctx.staging.csv_name(level, model_type)
        for level in (LEVEL_SUB, LEVEL_SUB + 1)
        for model_type in (_COORDINATE_TYPE_NAME, _ADDRESS_TYPE_NAME, _LOCATION_TYPE_NAME)
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
SHEET_BUILDERS: tuple[Callable[[Context], Sheet | None], ...] = (
    source_device,
    communications,
    messages,
    calls,
    emails,
    contacts,
    locations,
    identifiers,
    cdr_importer,
)
