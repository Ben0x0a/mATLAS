"""Check that the workbook found what the report holds, instead of showing blanks silently.

Defines:    REQUIRED_FIELDS and find_mapping_problems (one message per problem found).
Used by:    workbook.brick (runs them after building the sheets; the messages are raised as
            errors and shown on the About sheet).
Depends on: workbook.sheets (Context), workbook.comms (table and category names), const.

WHY: the raw dump is generic (it writes whatever models the report holds) but the sheets read
specific names: ``Coordinate.Latitude``, ``Party.Identifier``, Role ``From``, messages in a Chat.
If Cellebrite renames one, nothing fails: the sheet simply has a blank column, or misses rows.

Each check needs positive evidence from the data, because a missing model alone proves nothing (a
phone with no email is normal): a model HAS rows but lacks a field the sheets need (a report writes
even an empty field, so a missing column means a change, not missing data); a result column is
blank on EVERY row; or a model the workbook does not read has the SHAPE of one it does.
"""
from __future__ import annotations

from ufdr_parser.bricks.workbook.comms import CHAT_TYPE_NAME, COMMUNICATION_TYPES
from ufdr_parser.bricks.workbook.sheets import Context
from ufdr_parser.const import LEVEL_SUB, LEVEL_SUBSUB, LEVEL_TOP

_PARTY = "Party"
_COORDINATE = "Coordinate"
_LOCATION = "Location"
_JOURNEY = "Journey"
_BODY = "Body"
_DURATION = "Duration"
_CHANGED = "The report's structure may have changed (e.g. a new Cellebrite version)."

# (level, model) -> fields the sheets rely on. Levels matter: a quoted message nested at level 2
# legitimately has no TimeStamp, while a chat message at level 1 always has one.
REQUIRED_FIELDS: dict[tuple[int, str], tuple[str, ...]] = {
    (LEVEL_SUB, _PARTY): ("Identifier", "Role"),
    (LEVEL_SUBSUB, _PARTY): ("Identifier", "Role"),
    (LEVEL_SUB, _COORDINATE): ("Latitude", "Longitude"),
    (LEVEL_SUBSUB, _COORDINATE): ("Latitude", "Longitude"),
    (LEVEL_TOP, _LOCATION): ("TimeStamp",),
    (LEVEL_TOP, "Call"): ("TimeStamp",),
    (LEVEL_TOP, "Email"): ("TimeStamp",),
    (LEVEL_TOP, "InstantMessage"): ("TimeStamp", _BODY),
    (LEVEL_SUB, "InstantMessage"): ("TimeStamp", _BODY),
    (LEVEL_TOP, "DeviceInfoEntry"): ("EntryName", "EntryValue"),
}


def _required_fields(ctx: Context) -> list[str]:
    problems = []
    for (level, model), fields in REQUIRED_FIELDS.items():
        columns, has_rows = ctx.staging.peek(level, model)
        if not has_rows:
            continue
        csv_name = ctx.staging.csv_name(level, model)
        missing = [name for name in fields if name not in columns]
        if missing:
            problems.append(
                f"{model} ({csv_name}) has records but no {', '.join(missing)} field: the sheets "
                f"reading it leave those values blank. {_CHANGED}"
            )
    return problems


def _no_sender_anywhere(ctx: Context) -> list[str]:
    rows, with_sender = ctx.staging.conn.execute("SELECT COUNT(*), COUNT(sender) FROM comm").fetchone()
    if rows and not with_sender:
        return [
            f"None of the {rows} communication(s) has a sender: their parties could not be read "
            f"(Party model, its fields, or its Role values). {_CHANGED}"
        ]
    return []


def _location_without_coordinate(ctx: Context) -> list[str]:
    _columns, has_rows = ctx.staging.peek(LEVEL_TOP, _LOCATION)
    if has_rows and (_COORDINATE, _LOCATION) not in ctx.relations:
        children = sorted(child for child, top in ctx.relations if top == _LOCATION)
        return [
            f"Location records exist but none has a Coordinate, so no position can be read "
            f"(their child models: {', '.join(children) or 'none'}). {_CHANGED}"
        ]
    return []


def _journey_points_not_recognised(ctx: Context) -> list[str]:
    """A journey's points are read as its Location children; another type would lose them."""
    if (_COORDINATE, _JOURNEY) in ctx.relations and (_LOCATION, _JOURNEY) not in ctx.relations:
        children = sorted(child for child, top in ctx.relations if top == _JOURNEY and child != _COORDINATE)
        return [
            f"Journeys hold coordinates but no Location points, so their points cannot be read "
            f"(their child models: {', '.join(children) or 'none'}). {_CHANGED}"
        ]
    return []


def _chat_messages_not_recognised(ctx: Context) -> list[str]:
    """Chats holding message-shaped children of a type the workbook does not read."""
    problems = []
    for child, top in sorted(ctx.relations):
        if top == CHAT_TYPE_NAME and child not in COMMUNICATION_TYPES:
            columns, has_rows = ctx.staging.peek(LEVEL_SUB, child)
            if has_rows and _BODY in columns:
                problems.append(
                    f"Chats hold {child} records with a {_BODY} field, which the workbook does not "
                    f"read as messages. {_CHANGED}"
                )
    return problems


def _unread_lookalikes(ctx: Context) -> list[str]:
    """Top-level models shaped like a communication or a contact, under a name not read."""
    problems = []
    tops = {model for level, model in ctx.staging.groups() if level == LEVEL_TOP}
    with_parties = {top for child, top in ctx.relations if child == _PARTY}
    for model in sorted(tops - COMMUNICATION_TYPES - set(ctx.config.not_communications)):
        columns, has_rows = ctx.staging.peek(LEVEL_TOP, model)
        if has_rows and model in with_parties and (_BODY in columns or _DURATION in columns):
            problems.append(
                f"{model} records have parties and a {_BODY if _BODY in columns else _DURATION} field "
                f"but are not on the communication sheets. If {model} is not a communication, add it "
                "to [checks].not_communications in ufdr_config.toml."
            )
    return problems


def find_mapping_problems(ctx: Context) -> list[str]:
    """Every sign that a part of the report did not reach the sheets, as readable messages."""
    return (
        _required_fields(ctx)
        + _no_sender_anywhere(ctx)
        + _location_without_coordinate(ctx)
        + _journey_points_not_recognised(ctx)
        + _chat_messages_not_recognised(ctx)
        + _unread_lookalikes(ctx)
    )
