"""Resolve every communication once into a staged ``comm`` table.

Defines:    the communication categories, COMM_FIELDS, CommSource (where one category's records
            live) and build_comm_tables.
Used by:    workbook.sheets (Communications, Messages, Calls, Emails, CDRImporter).
Depends on: workbook.staging, workbook.links, const (column names).

WHY one shared table: five sheets show the same communications from different angles. Doing
the party resolution and To inference ONCE guarantees they agree row for row, and lets the
unified sheet sort every category together by time in SQL instead of in RAM.

Which records are communications (structure observed in Physical Analyzer reports):
- ``Call``, ``Email``, ``InstantMessage`` at the top level, with ``SMParty`` children;
- a ``Chat`` (top level) whose messages are level-1 ``InstantMessage`` children, each with
  ``SSMParty`` children; the Chat's own ``SMParty`` children are its participants.
A message nested one level deeper (``SSMInstantMessage``, e.g. a forwarded/quoted message) is
not a communication of its own and stays in the raw CSV.
"""
from __future__ import annotations

from dataclasses import dataclass

from ufdr_parser.const import (
    COL_MAIN_UUID,
    COL_SUB_UUID,
    COL_UUID,
    LEVEL_SUB,
    LEVEL_TOP,
    SOURCE_COLUMNS,
    TIMESTAMP_OFFSET_SUFFIX,
    TRACE_COLUMNS,
)
from ufdr_parser.bricks.workbook.links import (
    Links,
    join_identifiers,
    join_keys,
    join_labels,
    join_others,
    resolve_parties,
)
from ufdr_parser.bricks.workbook.staging import Staging, Table, quote

CATEGORY_CALL = "Call"
CATEGORY_EMAIL = "Email"
CATEGORY_INSTANT_MESSAGE = "Instant message"
CATEGORY_CHAT_MESSAGE = "Chat message"

CHAT_TYPE_NAME = "Chat"
# The chat's own identifier field (e.g. a WhatsApp group id), used as the CDR "B" of a group.
CHAT_ID_FIELD_NAME = "Id"
TIMESTAMP_FIELD_NAME = "TimeStamp"
# The one time order of the comm table, shared by its index and the sheets reading it.
TIME_ORDER_SQL = "timestamp IS NULL, timestamp, seq"
# CDR B_basis of a group message (its B_MSISDN is the group). WHY the group rather than every
# member: one CDR row per member multiplied a group's messages by its size (2,000 messages in a
# 200-member group = 398,000 rows); real reports reached tens of millions of rows, hours of
# writing and a temporary sort that filled the disk.
GROUP_BASIS = "{basis}; group chat ({count} recipients, listed in To on the Communications sheet)"

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
    "sender",
    "sender_ids",
    "sender_keys",
    "recipients",
    "recipient_ids",
    "to_basis",
    "others",
    "participants",
    "attachments",
    "cdr_b",
    "cdr_b_basis",
) + SOURCE_COLUMNS + TRACE_COLUMNS


@dataclass(frozen=True)
class CommSource:
    """Where one category's records live."""

    category: str
    level: int
    model_type: str


COMM_SOURCES = (
    CommSource(CATEGORY_CHAT_MESSAGE, LEVEL_SUB, "InstantMessage"),
    CommSource(CATEGORY_INSTANT_MESSAGE, LEVEL_TOP, "InstantMessage"),
    CommSource(CATEGORY_CALL, LEVEL_TOP, "Call"),
    CommSource(CATEGORY_EMAIL, LEVEL_TOP, "Email"),
)


def _base_select(source: CommSource, table: Table, chat: Table | None) -> str:
    """SELECT of one category's raw fields, joined to its chat for a chat message."""
    id_column = COL_UUID if source.level == LEVEL_TOP else COL_SUB_UUID
    is_chat = source.category == CATEGORY_CHAT_MESSAGE
    fields = [
        "m.seq",
        table.col(id_column, "m"),
        f"c.{quote(COL_UUID)}" if is_chat else "NULL",
        chat.col("Name", "c") if is_chat else "NULL",
        chat.col("ChatType", "c") if is_chat else "NULL",
        chat.col("Source", "c") if is_chat else "NULL",
        chat.col(CHAT_ID_FIELD_NAME, "c") if is_chat else "NULL",
        table.col(TIMESTAMP_FIELD_NAME, "m"),
        table.col(TIMESTAMP_FIELD_NAME + TIMESTAMP_OFFSET_SUFFIX, "m"),
        table.col("Source", "m"),  # verbatim: no fallback to SourceApplication or the chat
        table.col("Direction", "m"),
        table.col("Status", "m"),
        table.col("Subject", "m"),
        table.col("Body", "m"),
        table.col("Duration", "m"),
    ] + [table.col(column, "m") for column in SOURCE_COLUMNS + TRACE_COLUMNS]
    sql = f"SELECT {', '.join(fields)} FROM {table.name} AS m"
    if is_chat:
        # Inner join: a level-1 InstantMessage under anything other than a Chat is not a
        # chat message (none observed, but the join keeps the category honest).
        sql += f" JOIN {chat.name} AS c ON c.{quote(COL_UUID)} = m.{quote(COL_MAIN_UUID)}"
        return sql + " ORDER BY c.seq, m.seq"
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
    chat = staging.table(LEVEL_TOP, CHAT_TYPE_NAME)
    used: dict[str, list[str]] = {}
    for source in COMM_SOURCES:
        table = staging.table(source.level, source.model_type)
        if table is None or (source.category == CATEGORY_CHAT_MESSAGE and chat is None):
            continue
        used[source.category] = [staging.csv_name(source.level, source.model_type)]
        if source.category == CATEGORY_CHAT_MESSAGE:
            used[source.category].append(staging.csv_name(LEVEL_TOP, CHAT_TYPE_NAME))
        participants_cache: tuple[str | None, list] = (None, [])
        # Streamed from its own cursor: the inserts below touch only comm/recipient, never the
        # tables this SELECT reads, and nothing commits mid-loop, so the cursor stays valid
        # without materialising a category (chat bodies can run to GBs) in RAM.
        for raw in conn.cursor().execute(_base_select(source, table, chat)):
            row_seq, record_id, chat_id, *rest = raw
            participants = None
            if source.category == CATEGORY_CHAT_MESSAGE:
                # Rows arrive grouped by chat, so one cached chat suffices.
                if participants_cache[0] != chat_id:
                    participants_cache = (chat_id, links.parties(chat_id))
                participants = participants_cache[1]
            view = resolve_parties(links.parties(record_id), participants)
            (chat_name, chat_type, chat_source, chat_key, timestamp, offset, app, direction,
             status, subject, body, duration, *trace) = rest
            if source.category == CATEGORY_CHAT_MESSAGE and len(view.recipients) > 1:
                # A group message: its CDR recipient is the GROUP, not a list of every member
                # (the members stay in the To cell of the Messages / Communications sheets).
                cdr_b = chat_key or chat_name or chat_id
                cdr_b_basis = GROUP_BASIS.format(basis=view.to_basis, count=len(view.recipients))
            else:
                cdr_b, cdr_b_basis = join_keys(view.recipients), view.to_basis
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
                    join_labels(view.sender),
                    join_identifiers(view.sender),
                    join_keys(view.sender),
                    join_labels(view.recipients),
                    join_identifiers(view.recipients),
                    view.to_basis,
                    join_others(view.others),
                    join_labels(view.participants),
                    links.attachments(record_id),
                    cdr_b,
                    cdr_b_basis,
                    *trace,
                ),
            )
    conn.execute("CREATE INDEX ix_comm_record ON comm(record_id)")
    # Serves the time-ordered reads (Communications, Calls, Emails, CDRImporter) from the index
    # instead of a temporary sort holding a copy of every row (bodies included).
    conn.execute(f"CREATE INDEX ix_comm_time ON comm({TIME_ORDER_SQL})")
    return used
