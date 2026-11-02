"""Resolve every communication once into a staged ``comm`` table.

Defines:    the communication categories, COMM_FIELDS, CommSource (where one category's records
            live) and build_comm_tables.
Used by:    workbook.sheets (Communications).
Depends on: workbook.staging, workbook.links, const (column names).

WHY a staged table: it lets the sheet sort every category together by time in SQL instead of in
RAM (chat bodies can run to GBs).

Which records are communications (structure observed in Physical Analyzer reports):
- ``Call``, ``Email``, ``InstantMessage`` at the top level, with ``SMParty`` children;
- a ``Chat`` (top level) whose messages are level-1 ``InstantMessage`` children, each with
  ``SSMParty`` children; the Chat's own ``SMParty`` children are its participants.
The same models anywhere else (a quoted message, a related call...) are communications too, listed
as ``Nested ...`` with their parent's id, and a Chat at any level gives its messages their chat. A
chat holding no message gets a row of its own, so that no record of these models is left out.
"""
from __future__ import annotations

from dataclasses import dataclass

from ufdr_parser.const import (
    COL_MAIN_UUID,
    COL_SUB_UUID,
    COL_UUID,
    LEVEL_TOP,
    SOURCE_COLUMNS,
    TIMESTAMP_OFFSET_SUFFIX,
    TRACE_COLUMNS,
)
from ufdr_parser.bricks.workbook.links import (
    Links,
    join_identifiers,
    join_labels,
    join_others,
    resolve_parties,
)
from ufdr_parser.bricks.workbook.staging import Staging, Table, quote

CATEGORY_CALL = "Call"
CATEGORY_EMAIL = "Email"
CATEGORY_INSTANT_MESSAGE = "Instant message"
CATEGORY_CHAT_MESSAGE = "Chat message"
CATEGORY_NESTED_CALL = "Nested call"
CATEGORY_NESTED_EMAIL = "Nested email"
CATEGORY_NESTED_MESSAGE = "Nested message"
CATEGORY_EMPTY_CHAT = "Chat without message"

CHAT_TYPE_NAME = "Chat"
MESSAGE_TYPE_NAME = "InstantMessage"
TIMESTAMP_FIELD_NAME = "TimeStamp"
# The one time order of the comm table, shared by its index and the sheets reading it.
TIME_ORDER_SQL = "timestamp IS NULL, timestamp, seq"
# Resolved per-communication fields, in ``comm`` column order (after seq / table / row_seq).
COMM_FIELDS = (
    "category",
    "record_id",
    "chat_id",
    "chat_name",
    "chat_type",
    "chat_source",
    "timestamp",
    "timestamp_offset",
    "source",
    "direction",
    "status",
    "subject",
    "body",
    "duration",
    "parent_id",
    "sender",
    "sender_ids",
    "recipients",
    "recipient_ids",
    "to_basis",
    "others",
    "participants",
    "attachments",
) + SOURCE_COLUMNS + TRACE_COLUMNS


@dataclass(frozen=True)
class CommSource:
    """Where one category's records live.

    ``in_chat`` selects the messages of a Chat one level up; ``chat_record`` the Chats themselves.
    """

    category: str
    level: int
    model_type: str
    in_chat: bool = False
    chat_record: bool = False


# Communication model -> its category at the top level, and nested anywhere else.
_COMM_MODELS = {
    MESSAGE_TYPE_NAME: (CATEGORY_INSTANT_MESSAGE, CATEGORY_NESTED_MESSAGE),
    "Call": (CATEGORY_CALL, CATEGORY_NESTED_CALL),
    "Email": (CATEGORY_EMAIL, CATEGORY_NESTED_EMAIL),
}
COMMUNICATION_TYPES = frozenset(_COMM_MODELS) | {CHAT_TYPE_NAME}


def comm_sources(staging: Staging) -> list[CommSource]:
    """Every group of communication records the dump wrote, chat messages first."""
    sources: list[CommSource] = []
    for level, model_type in staging.groups():
        if model_type == CHAT_TYPE_NAME:
            sources.append(CommSource(CATEGORY_EMPTY_CHAT, level, model_type, chat_record=True))
        if model_type not in _COMM_MODELS:
            continue
        top_category, nested_category = _COMM_MODELS[model_type]
        if level == LEVEL_TOP:
            sources.append(CommSource(top_category, level, model_type))
            continue
        if model_type == MESSAGE_TYPE_NAME and staging.table(level - 1, CHAT_TYPE_NAME) is not None:
            sources.append(CommSource(CATEGORY_CHAT_MESSAGE, level, model_type, in_chat=True))
        sources.append(CommSource(nested_category, level, model_type))
    return sorted(sources, key=lambda source: (source.chat_record, not source.in_chat, source.level, source.model_type))


def _id_column(level: int) -> str:
    return COL_UUID if level == LEVEL_TOP else COL_SUB_UUID


def _base_select(source: CommSource, table: Table, staging: Staging) -> str:
    """SELECT of one category's raw fields, joined to its chat for a chat message."""
    # The chat one level up, for what may sit in one; the record itself when it is a chat.
    chat_level = source.level if source.chat_record else source.level - 1
    chat = staging.table(chat_level, CHAT_TYPE_NAME) if chat_level >= LEVEL_TOP else None
    with_chat = source.in_chat or source.chat_record
    fields = [
        "m.seq",
        table.col(_id_column(source.level), "m"),
        chat.col(_id_column(chat_level), "c") if with_chat else "NULL",
        chat.col("Name", "c") if with_chat else "NULL",
        chat.col("ChatType", "c") if with_chat else "NULL",
        chat.col("Source", "c") if with_chat else "NULL",
        table.col(TIMESTAMP_FIELD_NAME, "m"),
        table.col(TIMESTAMP_FIELD_NAME + TIMESTAMP_OFFSET_SUFFIX, "m"),
        table.col("Source", "m"),  # verbatim: no fallback to SourceApplication or the chat
        table.col("Direction", "m"),
        table.col("Status", "m"),
        table.col("Subject", "m"),
        table.col("Body", "m"),
        table.col("Duration", "m"),
        table.col(COL_MAIN_UUID, "m"),
    ] + [table.col(column, "m") for column in SOURCE_COLUMNS + TRACE_COLUMNS]
    sql = f"SELECT {', '.join(fields)} FROM {table.name} AS m"
    if source.chat_record:
        sql += f" JOIN {chat.name} AS c ON c.seq = m.seq"
        messages = staging.table(source.level + 1, MESSAGE_TYPE_NAME)
        if messages is not None:
            sql += (
                f" WHERE m.{quote(_id_column(source.level))} NOT IN "
                f"(SELECT {quote(COL_MAIN_UUID)} FROM {messages.name} WHERE {quote(COL_MAIN_UUID)} IS NOT NULL)"
            )
        return sql + " ORDER BY m.seq"
    if source.in_chat:
        sql += f" JOIN {chat.name} AS c ON c.{quote(_id_column(chat_level))} = m.{quote(COL_MAIN_UUID)}"
        return sql + " ORDER BY c.seq, m.seq"
    if chat is not None and source.model_type == MESSAGE_TYPE_NAME:
        # What the in_chat source does not take: a message under anything but a Chat.
        sql += f" WHERE m.{quote(COL_MAIN_UUID)} NOT IN (SELECT {quote(_id_column(chat_level))} FROM {chat.name})"
    return sql + " ORDER BY m.seq"


def build_comm_tables(staging: Staging, links: Links) -> dict[str, list[str]]:
    """Create and fill ``comm``; return {category: [csv file names used]}."""
    conn = staging.conn
    conn.execute(
        "CREATE TABLE comm (seq INTEGER PRIMARY KEY, raw_table TEXT, row_seq INTEGER, "
        + ", ".join(f"{quote(name)} TEXT" for name in COMM_FIELDS)
        + ")"
    )
    insert = (
        f"INSERT INTO comm (raw_table, row_seq, {', '.join(quote(n) for n in COMM_FIELDS)}) "
        f"VALUES ({', '.join('?' for _ in range(len(COMM_FIELDS) + 2))})"
    )
    used: dict[str, list[str]] = {}
    for source in comm_sources(staging):
        table = staging.table(source.level, source.model_type)
        if table is None:
            continue
        used.setdefault(source.category, []).append(staging.csv_name(source.level, source.model_type))
        if source.in_chat:
            used[source.category].append(staging.csv_name(source.level - 1, CHAT_TYPE_NAME))
        participants_cache: tuple[str | None, list] = (None, [])
        # Streamed from its own cursor: the inserts below touch only comm/recipient, never the
        # tables this SELECT reads, and nothing commits mid-loop, so the cursor stays valid
        # without materialising a category (chat bodies can run to GBs) in RAM.
        for raw in conn.cursor().execute(_base_select(source, table, staging)):
            row_seq, record_id, chat_id, *rest = raw
            participants = None
            if source.in_chat or source.chat_record:
                # Rows arrive grouped by chat, so one cached chat suffices.
                if participants_cache[0] != chat_id:
                    participants_cache = (chat_id, links.parties(chat_id))
                participants = participants_cache[1]
            # A chat's own parties are its participants, not the parties of one event.
            view = resolve_parties([] if source.chat_record else links.parties(record_id), participants)
            (chat_name, chat_type, chat_source, timestamp, offset, app, direction,
             status, subject, body, duration, parent_id, *trace) = rest
            conn.execute(
                insert,
                (
                    table.name,
                    row_seq,
                    source.category,
                    record_id,
                    chat_id,
                    chat_name,
                    chat_type,
                    chat_source,
                    timestamp,
                    offset,
                    app,
                    direction,
                    status,
                    subject,
                    body,
                    duration,
                    parent_id,
                    join_labels(view.sender),
                    join_identifiers(view.sender),
                    join_labels(view.recipients),
                    join_identifiers(view.recipients),
                    view.to_basis,
                    join_others(view.others),
                    join_labels(view.participants),
                    links.attachments(record_id),
                    *trace,
                ),
            )
    conn.execute("CREATE INDEX ix_comm_record ON comm(record_id)")
    # Serves the time-ordered read of the Communications sheet from the index instead of a temporary sort holding a copy of every row (bodies included).
    conn.execute(f"CREATE INDEX ix_comm_time ON comm({TIME_ORDER_SQL})")
    return used
