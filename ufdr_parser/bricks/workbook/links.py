"""Resolve the links between dumped records: parties, attachments, and who-wrote-to-whom.

Defines:    Party, PartyView, Links (indexed party/attachment lookups over the staging DB),
            format_party, and resolve_parties (From / To / other parties of one
            communication, with the explicitly flagged To inference).
Used by:    workbook.sheets.
Depends on: workbook.staging, const (column names), sqlite3.

The report nests a communication's parties as child ``Party`` models (``SMParty`` under a
Call/Email/InstantMessage/Chat, ``SSMParty`` under a chat's message), linked by ``main-uuid``.
Every Party group is folded into one ``party`` table keyed by its parent id, so each lookup is
one index probe whatever the nesting level. Attachments are folded the same way.

TO INFERENCE (mirrors the legacy UFEDParser, but never silently): a chat message often states
only its sender. When it states NO recipient, its chat's participants are used — every
participant other than the sender — and the row's ``To basis`` says so. It is only done when
the sender is itself among the participants; otherwise "the others" cannot be told apart from
the sender and To is left blank. Calls, emails and standalone messages are never inferred.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ufdr_parser.const import COL_MAIN_UUID, LEVEL_SUB, LEVEL_SUBSUB, MULTI_VALUE_SEPARATOR
from ufdr_parser.bricks.workbook.staging import Staging, quote

PARTY_TYPE_NAME = "Party"
ATTACHMENT_TYPE_NAME = "Attachment"

ROLE_FROM = "From"
ROLE_TO = "To"

# ``To basis`` values: where the To column came from.
TO_BASIS_STATED = "stated"
TO_BASIS_INFERRED = "inferred from chat participants"
TO_BASIS_NONE = "not stated"

# Cellebrite's IsPhoneOwner flag, worded as what it is: the report's attribution (typically an
# account or number used on this device), not a proof of who owns the phone.
OWNER_MARK = " [flagged as phone owner]"


@dataclass(frozen=True)
class Party:
    identifier: str | None
    name: str | None
    role: str | None
    is_owner: bool

    def label(self) -> str:
        return format_party(self.identifier, self.name, self.is_owner)

    def key(self) -> str | None:
        """What identifies the party in an identifier-only column: identifier, else name."""
        return self.identifier or self.name


@dataclass
class PartyView:
    """The resolved parties of one communication, ready to write."""

    sender: list[Party] = field(default_factory=list)  # mutable default — one list per view
    recipients: list[Party] = field(default_factory=list)  # mutable default — one list per view
    others: list[Party] = field(default_factory=list)  # mutable default — one list per view
    participants: list[Party] = field(default_factory=list)  # mutable default — one list per view
    to_basis: str = TO_BASIS_NONE


def format_party(identifier: str | None, name: str | None, is_owner: bool) -> str:
    """``Name <Identifier>`` (or whichever is known), marked when the report flags it as the
    phone owner.

    The identifier is kept verbatim (a leading ``+`` is part of the number — the legacy tool
    stripped it, which made international numbers ambiguous).
    """
    if name and identifier and name != identifier:
        text = f"{name} <{identifier}>"
    else:
        text = identifier or name or ""
    return text + OWNER_MARK if is_owner and text else text


def join_labels(parties: list[Party]) -> str | None:
    labels = [party.label() for party in parties if party.label()]
    return MULTI_VALUE_SEPARATOR.join(labels) if labels else None


def join_identifiers(parties: list[Party]) -> str | None:
    values = [party.identifier for party in parties if party.identifier]
    return MULTI_VALUE_SEPARATOR.join(values) if values else None


def join_keys(parties: list[Party]) -> str | None:
    values = [key for party in parties if (key := party.key())]
    return MULTI_VALUE_SEPARATOR.join(values) if values else None


def join_others(parties: list[Party]) -> str | None:
    """Parties with a role other than From/To, each prefixed by its role (lossless)."""
    labels = [f"[{party.role or 'no role'}] {party.label()}" for party in parties if party.label()]
    return MULTI_VALUE_SEPARATOR.join(labels) if labels else None


def resolve_parties(own: list[Party], participants: list[Party] | None) -> PartyView:
    """Split a communication's own parties by role and, for a chat message, infer To.

    ``participants`` is the chat's party list for a chat message, None for anything else.
    """
    # A Party with neither identifier nor name says nothing about who took part; counting it
    # would mark a recipient as "stated" while the To cell stays blank.
    own = [party for party in own if party.key()]
    view = PartyView(participants=[party for party in participants or [] if party.key()])
    for party in own:
        if party.role == ROLE_FROM:
            view.sender.append(party)
        elif party.role == ROLE_TO:
            view.recipients.append(party)
        else:
            view.others.append(party)
    if view.recipients:
        view.to_basis = TO_BASIS_STATED
        return view
    if participants is None:
        return view
    sender_ids = {party.identifier for party in view.sender if party.identifier}
    participant_ids = {party.identifier for party in view.participants if party.identifier}
    # Only when the sender is a known participant can "everyone else" be the recipients;
    # otherwise the sender could be one of them and the inference would be a guess.
    if sender_ids and sender_ids <= participant_ids:
        seen: set[str] = set()
        for party in view.participants:
            if party.identifier and party.identifier not in sender_ids and party.identifier not in seen:
                seen.add(party.identifier)
                view.recipients.append(party)
        if view.recipients:
            view.to_basis = TO_BASIS_INFERRED
    return view


class Links:
    """Indexed lookups from a record id to its parties and attachments."""

    def __init__(self, staging: Staging) -> None:
        self._conn: sqlite3.Connection = staging.conn
        self._fold(
            staging,
            PARTY_TYPE_NAME,
            "party",
            ("Identifier", "Name", "Role", "IsPhoneOwner"),
        )
        self._fold(
            staging,
            ATTACHMENT_TYPE_NAME,
            "attachment",
            ("Filename", "attachment_extracted_path", "URL"),
        )

    def _fold(
        self, staging: Staging, model_type: str, target: str, columns: tuple[str, ...]
    ) -> None:
        """Union the level-1 and level-2 groups of ``model_type`` into one parent-keyed table."""
        column_sql = ", ".join(f"c{i} TEXT" for i in range(len(columns)))
        self._conn.execute(
            f"CREATE TABLE {target} (seq INTEGER PRIMARY KEY, parent_id TEXT, {column_sql})"
        )
        for level in (LEVEL_SUB, LEVEL_SUBSUB):
            table = staging.table(level, model_type)
            if table is None:
                continue
            select = ", ".join(table.col(column) for column in columns)
            self._conn.execute(
                f"INSERT INTO {target} (parent_id, "
                f"{', '.join(f'c{i}' for i in range(len(columns)))}) "
                f"SELECT {quote(COL_MAIN_UUID)}, {select} FROM {table.name} ORDER BY seq"
            )
        self._conn.execute(f"CREATE INDEX ix_{target}_parent ON {target}(parent_id)")

    def parties(self, parent_id: str | None) -> list[Party]:
        if not parent_id:
            return []
        rows = self._conn.execute(
            "SELECT c0, c1, c2, c3 FROM party WHERE parent_id = ? ORDER BY seq", (parent_id,)
        )
        return [
            Party(identifier=ident, name=name, role=role, is_owner=(owner or "").lower() == "true")
            for ident, name, role, owner in rows
        ]

    def attachments(self, parent_id: str | None) -> str | None:
        """The parent's attachments, one ``filename (extracted path)`` entry each."""
        if not parent_id:
            return None
        labels = []
        for filename, extracted, url in self._conn.execute(
            "SELECT c0, c1, c2 FROM attachment WHERE parent_id = ? ORDER BY seq", (parent_id,)
        ):
            name = filename or url or "(unnamed)"
            labels.append(f"{name} ({extracted})" if extracted else name)
        return MULTI_VALUE_SEPARATOR.join(labels) if labels else None

    def all_parties(self) -> "sqlite3.Cursor":
        """Every party row as (parent_id, identifier, name, role, is_owner), in order."""
        return self._conn.execute("SELECT parent_id, c0, c1, c2, c3 FROM party ORDER BY seq")
