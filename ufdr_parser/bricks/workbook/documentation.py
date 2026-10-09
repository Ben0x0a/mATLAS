"""Static documentation of the combined workbook: every sheet, every column, the general rules.

Defines:    Column, SheetDoc, expand_header, the per-sheet SheetDoc constants (SHEET_DOCS, in
            workbook order) and GENERAL_NOTES.
Used by:    workbook.sheets (each builder takes its header from its SheetDoc) and
            workbook.brick (writes the Documentation sheet from SHEET_DOCS).
Depends on: const (column names), workbook.config (Contacts identifier columns), workbook.links
            (party / To-basis vocabulary).

WHY a separate, static description: what a sheet means does not depend on the report, so it is
written once, identical in every workbook, while the About sheet holds only what does depend
on the report (file, row counts, CSVs used). WHY the builders read their headers from here: the
documentation is then the definition of the layout rather than a copy of it, so a column cannot
be added to a sheet without being documented.

Columns that vary with the report (a record's own fields, a Location's other Position fields,
an unusual Contact child type) are documented once by a placeholder Column whose ``block``
names the dynamic block a builder expands in its place.

ORIGIN of a column: ``report`` = a value copied verbatim from the report (through its CSV);
``tool`` = computed here (joined, formatted, classified, inferred) or added by the dumper (a
``.offset`` column, report_file/report_entry). Every written header starts with its origin
(REPORT_COLUMN_PREFIX / TOOL_COLUMN_PREFIX), so the origin is visible on the sheet itself, not
only in the documentation. Internally columns keep their bare name; only the header carries it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ufdr_parser.bricks.workbook.config import (
    CONTACT_APP_COLUMNS,
    CONTACT_EMAIL_COLNAME,
    CONTACT_OTHER_IDS_COLNAME,
    CONTACT_PHONE_COLNAME,
)
from ufdr_parser.bricks.workbook.links import (
    OWNER_MARK,
    TO_BASIS_INFERRED,
    TO_BASIS_NONE,
    TO_BASIS_STATED,
)
from ufdr_parser.const import (
    COL_REPORT_ENTRY,
    COL_REPORT_FILE,
    COL_SOURCE_NAME,
    COL_SOURCE_OFFSET,
    COL_SOURCE_PATH,
    COL_SOURCE_SIZE,
    COL_SOURCE_TABLE,
    MULTI_VALUE_SEPARATOR,
    TIMESTAMP_OFFSET_SUFFIX,
)

# Dynamic block keys a builder expands (see expand_header).
BLOCK_OWN = "own"
BLOCK_COORDINATE = "coordinate"
BLOCK_ADDRESS = "address"
BLOCK_CHILDREN = "children"
BLOCK_JOURNEY = "journey"

ORIGIN_REPORT = "report"
ORIGIN_TOOL = "tool"
REPORT_COLUMN_PREFIX = "[report] "
TOOL_COLUMN_PREFIX = "[tool] "

RECORD_UUID_COLNAME = "Record uuid"
TIMESTAMP_COLNAME = "TimeStamp"
TIMESTAMP_OFFSET_COLNAME = f"{TIMESTAMP_COLNAME}{TIMESTAMP_OFFSET_SUFFIX}"
COORDINATE_PREFIX = "Coordinate."
ADDRESS_PREFIX = "StreetAddress."
JOURNEY_PREFIX = "Journey."


@dataclass(frozen=True)
class Column:
    """One documented column, or (``block`` set) a placeholder for report-dependent columns.

    ``field`` names the resolved communication field feeding the column (comms.COMM_FIELDS);
    ``origin`` says whether its values come verbatim from the report or from this tool.
    """

    name: str
    meaning: str
    field: str | None = None
    block: str | None = None
    origin: str = ORIGIN_REPORT
    # True for a name an external consumer depends on (the legacy CDR importer): shown
    # unprefixed even when computed by the tool; its origin is still documented.
    fixed_name: bool = False

    @property
    def display(self) -> str:
        return self.name if self.fixed_name else display_name(self.name, self.origin)


def display_name(name: str, origin: str) -> str:
    """The written header: the column name prefixed by its origin, so it shows on the sheet."""
    prefix = TOOL_COLUMN_PREFIX if origin == ORIGIN_TOOL else REPORT_COLUMN_PREFIX
    return f"{prefix}{name}"


def _tool(
    name: str, meaning: str, field: str | None = None, block: str | None = None, *, fixed_name: bool = False
) -> Column:
    return Column(name, meaning, field=field, block=block, origin=ORIGIN_TOOL, fixed_name=fixed_name)


@dataclass(frozen=True)
class SheetDoc:
    name: str
    row_is: str
    columns: tuple[Column, ...]
    notes: tuple[str, ...] = ()

    def static(self) -> list[Column]:
        return [column for column in self.columns if column.block is None]


def expand_header(
    doc: SheetDoc, blocks: dict[str, list[str]]
) -> tuple[list[str], list[str], dict[str, str]]:
    """The sheet header: static columns in place, each placeholder replaced by its block.

    Returns ``(header, display, renames)``. ``header`` holds the internal column keys the
    builders fill; ``display`` the names written on the sheet (tool columns prefixed — a dynamic
    ``<field>.offset`` column is the dumper's, so it is a tool column too); ``renames`` maps a
    dynamic column whose name collides with a static one to the ``<name> (record)`` key it is
    written under, so a raw field named like a curated column never overwrites it.
    """
    static_names = {column.name for column in doc.static()}
    header: list[str] = []
    display: list[str] = []
    renames: dict[str, str] = {}
    for column in doc.columns:
        if column.block is None:
            header.append(column.name)
            display.append(column.display)
            continue
        for name in blocks.get(column.block, []):
            shown = f"{name} (record)" if name in static_names else name
            if shown != name:
                renames[name] = shown
            header.append(shown)
            origin = ORIGIN_TOOL if name.endswith(TIMESTAMP_OFFSET_SUFFIX) else column.origin
            display.append(display_name(shown, origin))
    return header, display, renames


# --------------------------------------------------------------------------- shared columns

PROVENANCE_COLUMNS = (
    Column(RECORD_UUID_COLNAME, "Cellebrite id of the record (uuid / sub-uuid in the CSVs)."),
    Column(COL_SOURCE_PATH, "On-device file the record was decoded from (report <extraInfos>)."),
    Column(COL_SOURCE_NAME, "File name of source_path."),
    Column(COL_SOURCE_TABLE, "Database table(s) the record was read from, when decoded from a database."),
    Column(COL_SOURCE_OFFSET, "Byte offset of the record in source_path, as stated by the report."),
    Column(COL_SOURCE_SIZE, "Size in bytes of source_path, as stated by the report."),
    _tool(COL_REPORT_FILE, "The Cellebrite report file (.ufdr or .xml) the dump read."),
    _tool(COL_REPORT_ENTRY, "The report's entry inside the .ufdr archive (blank for a bare .xml)."),
)

_OFFSET_MEANING = (
    "UTC offset stated by TimeStamp; blank when no offset could be determined (the report gives "
    "the time without a zone). Never a default."
)
_TIME = (
    Column(TIMESTAMP_COLNAME, "When it happened, verbatim from the report.", field="timestamp"),
    _tool(TIMESTAMP_OFFSET_COLNAME, _OFFSET_MEANING, field="timestamp_offset"),
)
_PARTIES = (
    _tool("From", "Sender(s): the report's parties with Role From, formatted 'Name <Identifier>'.", field="sender"),
    _tool("To", "Recipient(s): the parties with Role To, or inferred (see To basis).", field="recipients"),
    _tool(
        "To basis",
        f"Where To comes from: '{TO_BASIS_STATED}', '{TO_BASIS_INFERRED}' or '{TO_BASIS_NONE}'.",
        field="to_basis",
    ),
    _tool("Other parties", "Parties with any other role (Cc, Bcc, General...), each prefixed by its [Role].", field="others"),
)
_ATTACHMENTS = _tool("Attachments", "Attachment file names, with the extracted path when given.", field="attachments")


def _own(record: str) -> Column:
    return Column(
        f"<other {record} fields>",
        f"Every other field of the {record} record, verbatim, named as in the CSV (only the "
        f"fields this report uses). A <Field>{TIMESTAMP_OFFSET_SUFFIX} column among them is added by "
        "the dumper: the UTC offset of the time field before it, blank when none could be determined.",
        block=BLOCK_OWN,
    )


# --------------------------------------------------------------------------- general rules

_PARTY_NOTE = (
    f"Parties are written 'Name <Identifier>' (or whichever is known), several joined with "
    f"'{MULTI_VALUE_SEPARATOR}'; '{OWNER_MARK.strip()}' marks a party the report flags as the "
    "phone owner (Cellebrite IsPhoneOwner): the report's attribution — typically an account or "
    "number used on this device — not a proof of ownership. Identifiers are verbatim (a leading + is kept). A party with neither "
    "identifier nor name is ignored."
)
_TO_NOTE = (
    f"To basis: '{TO_BASIS_STATED}' = recipients listed by the report; '{TO_BASIS_INFERRED}' = the "
    "message lists no recipient, so every chat participant other than the sender is shown (only "
    f"when the sender is one of the participants); '{TO_BASIS_NONE}' = no recipient in the report "
    "and none inferred. Only chat messages are ever inferred."
)
_DUPLICATE_NOTE = (
    "Each decoded record is listed once, as the report gives it: the same real message can be "
    "decoded from two databases (e.g. an Android SMS from the call-log and from the messages "
    "database, or an iOS message from Biome/KnowledgeC) and then appears twice, with a different "
    "source_path. Compare source_path before counting."
)
_SORT_NOTE = (
    "Sorted by the TimeStamp text. Reports normally state every time in the same offset (see the "
    ".offset column); rows in different offsets would not interleave correctly."
)

GENERAL_NOTES = (
    ("Column names", f"Every column name starts with its origin. '{REPORT_COLUMN_PREFIX.strip()}' = "
     "a value copied verbatim from the report. "
     f"'{TOOL_COLUMN_PREFIX.strip()}' = computed by this tool (joined, formatted, classified or "
     "inferred), or a .offset / report_* column added by the dumper. The Origin column below "
     "says the same for each column. CDRImporter keeps the exact legacy column names its "
     "importer expects; only its extra columns are prefixed."),
    ("What this workbook is", "A re-arrangement of the raw per-model CSVs written next to it, "
     "joining on one row what a reader needs (who, to whom, when, what). The CSVs remain the "
     "reference: every value is copied from them unchanged, except the joins and the To "
     "inference described here."),
    ("All cells are text", "Values are never converted: identifiers keep a leading + or 0, and "
     "text starting with = is never a formula."),
    ("Several values in one cell", f"Joined with '{MULTI_VALUE_SEPARATOR}'."),
    ("Parties", _PARTY_NOTE),
    ("To basis", _TO_NOTE),
    ("Duplicates", _DUPLICATE_NOTE),
    ("Time offsets", "Every time column is followed by its .offset column: the UTC offset the "
     "report states. Blank means no offset could be determined; no offset is ever assumed."),
    ("Excel limits", "A sheet longer than 1,048,576 rows continues on '<Sheet> (2)', '(3)'...; a "
     "cell longer than 32,767 characters is cut and ends with a marker giving the full length "
     "(the full text is in the CSV); a control character Excel cannot store is shown as "
     "[U+XXXX]. The About sheet counts each case."),
    ("Placeholder rows", "A column name in <angle brackets> below stands for columns that depend "
     "on the report (the fields its records actually carry)."),
)


# --------------------------------------------------------------------------- the sheets

COMMUNICATIONS = SheetDoc(
    "Communications",
    "One call, email, instant message or chat message — every category together.",
    (
        _tool("Category", "Call, Email, Instant message or Chat message.", field="category"),
        *_TIME,
        Column("Source", "The record's Source field, verbatim.", field="source"),
        Column("Direction", "Call direction, as stated by the report.", field="direction"),
        Column("Status", "Status, as stated by the report.", field="status"),
        *_PARTIES,
        _tool("From identifiers", "Sender identifier(s) only, for filtering.", field="sender_ids"),
        _tool("To identifiers", "Recipient identifier(s) only, for filtering.", field="recipient_ids"),
        Column("Chat", "Name of the chat a chat message belongs to.", field="chat_name"),
        Column("Subject", "Email subject.", field="subject"),
        Column("Body", "Message or email text.", field="body"),
        Column("Duration", "Call duration, as stated by the report.", field="duration"),
        _ATTACHMENTS,
        *PROVENANCE_COLUMNS,
    ),
    (_SORT_NOTE, "Per-category fields are on the Calls / Emails / Messages sheets."),
)

MESSAGES = SheetDoc(
    "Messages",
    "One message: chat messages grouped by chat (chats in report order), then standalone instant messages.",
    (
        _tool("Category", "Chat message (inside a Chat) or Instant message (standalone).", field="category"),
        Column("Chat", "Name of the chat the message belongs to.", field="chat_name"),
        Column("Chat type", "Chat type (e.g. OneOnOne, Group), as stated by the report.", field="chat_type"),
        Column("Chat app", "The chat's Source: the application the chat belongs to.", field="chat_source"),
        *_TIME,
        *_PARTIES,
        _tool("Chat participants", "Every party the chat itself lists, formatted 'Name <Identifier>'.", field="participants"),
        Column("Body", "Message text.", field="body"),
        _ATTACHMENTS,
        Column("Status", "Message status, as stated by the report.", field="status"),
        Column("Chat uuid", "Cellebrite id of the chat: filter on it to read one conversation.", field="chat_id"),
        _own("message"),
        *PROVENANCE_COLUMNS,
    ),
    (
        "Within a chat, messages are sorted by the TimeStamp text.",
        "A message nested inside another message (SSMInstantMessage: quoted or forwarded) is not "
        "listed; it remains in the raw CSV.",
    ),
)

CALLS = SheetDoc(
    "Calls",
    "One call.",
    (
        *_TIME,
        Column("Direction", "Call direction, as stated by the report.", field="direction"),
        Column("Status", "Call status, as stated by the report.", field="status"),
        Column("Duration", "Call duration, as stated by the report.", field="duration"),
        *_PARTIES,
        _own("Call"),
        *PROVENANCE_COLUMNS,
    ),
    (_SORT_NOTE, "To is never inferred for a call."),
)

EMAILS = SheetDoc(
    "Emails",
    "One email.",
    (
        *_TIME,
        Column("Status", "Email status, as stated by the report.", field="status"),
        *_PARTIES,
        Column("Subject", "Email subject.", field="subject"),
        Column("Body", "Email body.", field="body"),
        _ATTACHMENTS,
        _own("Email"),
        *PROVENANCE_COLUMNS,
    ),
    (_SORT_NOTE, "To is never inferred for an email; Cc, Bcc and other roles are in Other parties."),
)

# Contact child types with a fixed column of their own: (model type, column header).
CONTACT_CHILD_COLUMNS = (
    ("WebAddress", "Web addresses"),
    ("StreetAddress", "Addresses"),
    ("Organization", "Organizations"),
    ("ContactPhoto", "Photos"),
    ("ProfilePicture", "Profile pictures"),
    ("KeyValueModel", "Additional fields"),
)

CONTACTS = SheetDoc(
    "Contacts",
    "One contact, with its phone numbers, emails and per-application identifiers in cells.",
    (
        _own("Contact"),
        _tool(CONTACT_PHONE_COLNAME, "Every phone number of the contact ('Category: Value')."),
        _tool(CONTACT_EMAIL_COLNAME, "Every email address of the contact ('Category: Value')."),
        *(
            _tool(header, f"Every {header} identifier of the contact ('Category: Value'); blank when none.")
            for header, _pattern in CONTACT_APP_COLUMNS
        ),
        _tool(CONTACT_OTHER_IDS_COLNAME, "Every user ID matching none of the application columns ('Category: Value')."),
        *(
            _tool(header, f"Every {model_type} entry of the contact.")
            for model_type, header in CONTACT_CHILD_COLUMNS
        ),
        _tool(
            "<other child types>",
            "Any other kind of entry the contacts hold, one column per kind, named after it.",
            block=BLOCK_CHILDREN,
        ),
        *PROVENANCE_COLUMNS,
    ),
    (
        "A user ID goes to the application named by its Category (e.g. 'Instagram Id'), else by "
        "the contact's Source (e.g. 'Snapchat'), matching whole words; one matching no application "
        "goes to Other identifiers. The application list is set in "
        "ufdr_parser/bricks/workbook/config.py.",
        "Each entry is labelled with its Category, e.g. 'WhatsApp User Id: ...; Push Name: ...'.",
    ),
)

LOCATIONS = SheetDoc(
    "Locations",
    "One record that carries a position or describes a place (a Location, a cell tower, a "
    "Wi-Fi network, a journey point, a search, or any other record with a Coordinate), its "
    "fields flattened as the report gives them.",
    (
        _tool("Record type", "The Cellebrite model type of the record (Location, CellTower, "
              "WirelessNetwork, SearchedItem...); 'Journey point' for a Location inside a Journey."),
        Column("Latitude", "Latitude of the record, verbatim; Latitude field names the column it was copied from."),
        Column("Longitude", "Longitude of the record, verbatim; Longitude field names the column it was copied from."),
        _tool("Latitude field", "Where Latitude was copied from, as '<CSV>.<column>' (e.g. "
              "SMCoordinate.Latitude for a record's Coordinate, SSMCoordinate.Latitude for a journey "
              "point's); blank when the record has none."),
        _tool("Longitude field", "Where Longitude was copied from, same convention as Latitude field."),
        Column(
            f"<{COORDINATE_PREFIX}field>",
            "Every other field of the record's Coordinate (elevation, accuracy...), verbatim.",
            block=BLOCK_COORDINATE,
        ),
        Column(
            f"<{ADDRESS_PREFIX}field>",
            "Every field of the record's StreetAddress, verbatim (several addresses joined with "
            f"'{MULTI_VALUE_SEPARATOR}').",
            block=BLOCK_ADDRESS,
        ),
        Column(
            f"<{JOURNEY_PREFIX}field>",
            "For a journey point: every field of the journey it belongs to, verbatim.",
            block=BLOCK_JOURNEY,
        ),
        _own("location / cell / Wi-Fi / journey point / search"),
        *PROVENANCE_COLUMNS,
    ),
    (
        "Rows are in report order, type by type (no sorting): Location, CellTower, "
        "WirelessNetwork, Journey (its points in order), SearchedItem, then any other type that "
        "carries a Coordinate. The list of types is set in ufdr_parser/bricks/workbook/config.py.",
        "Every record of those configured types is listed, with blank Latitude/Longitude when it "
        "has no coordinate (a SearchedItem rarely has one: many are web searches); any other type "
        "(e.g. SocialMediaActivity) contributes only its records that have a Coordinate.",
        "A record with several coordinates gets one row per coordinate.",
    ),
)

SOURCE_DEVICE = SheetDoc(
    "Source device",
    "One value about the report, the acquisition or the device: the report header first "
    "(report, case, extraction, images), then the device information the report decoded.",
    (
        _tool("Section", "Where the value is in the report: a header section (Report, Case "
              "information, Source extraction, Metadata: ..., Image ...) or 'Device info: "
              "<category>' for a decoded DeviceInfoEntry."),
        Column("Name", "Name of the value as the report gives it (header attribute or item name, "
               "or the DeviceInfoEntry EntryName, e.g. IMEI, Serial, ICCID, IMSI, MSISDN)."),
        Column("Value", "The value, verbatim."),
        Column("Details", "Other attributes the report attaches to it (e.g. sourceExtraction, "
               "fieldType, hash section), or the DeviceInfoEntry's Source."),
        Column(TIMESTAMP_COLNAME, "TimeStamp of a DeviceInfoEntry, verbatim (blank for header values)."),
        _tool(TIMESTAMP_OFFSET_COLNAME, _OFFSET_MEANING),
        *PROVENANCE_COLUMNS,
    ),
    (
        "Header values come from the report_info CSV; device information from the "
        "DeviceInfoEntry CSV. Times in the header are kept in the report's own format (e.g. "
        "'03/09/2025 17:57:43(UTC+2)'), never converted.",
        "The same identifier (IMEI, ICCID, MSISDN...) can appear several times, decoded from "
        "different sources; source_path tells them apart.",
    ),
)

IDENTIFIERS = SheetDoc(
    "Identifiers",
    "One distinct party identifier, across every Party in the report.",
    (
        Column("Identifier", "Party identifier (phone number, handle, email...), verbatim."),
        _tool("Names", "Every distinct name the report gives this identifier."),
        _tool(
            "Flagged as phone owner",
            "'yes' when the report flags this identifier as the phone owner (Cellebrite "
            "IsPhoneOwner) at least once: the report's attribution, typically an account or "
            "number used on this device, not a proof of ownership.",
        ),
        _tool("Occurrences", "Number of Party entries carrying this identifier."),
        _tool("As sender", "How many of them have Role From."),
        _tool("As recipient", "How many of them have Role To (stated only, never inferred)."),
        _tool("Other roles", "Other roles with their counts (e.g. General = chat participant)."),
        _tool("Seen in", "Kinds of record the identifier appears in, with counts."),
        _tool("Sources", "Distinct Source values of those records (their Source field, verbatim)."),
        _tool("First seen", "Earliest TimeStamp text among those records."),
        _tool("Last seen", "Latest TimeStamp text among those records."),
    ),
    ("Parties with a name but no identifier are not listed (no key to group them on).",),
)

_CDR_BLANK = "Not available from a Cellebrite report; left blank (legacy CDR layout)."
CDR_IMPORTER = SheetDoc(
    "CDRImporter",
    "One sender -> recipient pair: a communication with N recipients gives N rows (one row, B blank, when it has none).",
    (
        _tool("A_MSISDN", "Sender identifier(s); the sender's name when the report gives no identifier.", field="sender_keys", fixed_name=True),
        _tool("A_IMEI", _CDR_BLANK, fixed_name=True),
        _tool("A_IMSI", _CDR_BLANK, fixed_name=True),
        _tool("A_IP", _CDR_BLANK, fixed_name=True),
        _tool("Type1", "Communication category (Call, Email, Instant message, Chat message).", field="category", fixed_name=True),
        _tool("Type2", "Call direction, as stated by the report (blank for messages).", field="direction", fixed_name=True),
        _tool("DateTime", "TimeStamp verbatim from the report.", field="timestamp", fixed_name=True),
        _tool("Duration", "Call duration, as stated by the report.", field="duration", fixed_name=True),
        _tool("B_MSISDN", "ONE recipient identifier (stated or inferred, see B_basis); its name when it has no identifier.", fixed_name=True),
        _tool("B_IMEI", _CDR_BLANK, fixed_name=True),
        _tool("B_IMSI", _CDR_BLANK, fixed_name=True),
        _tool("B_IP", _CDR_BLANK, fixed_name=True),
        _tool("C_MSISDN", _CDR_BLANK, fixed_name=True),
        _tool("Comment", "Message or email text (the email subject when there is no body).", fixed_name=True),
        _tool("LocOwner", _CDR_BLANK, fixed_name=True),
        _tool("start_Latitude", _CDR_BLANK, fixed_name=True),
        _tool("start_Longitude", _CDR_BLANK, fixed_name=True),
        _tool("start_Address", _CDR_BLANK, fixed_name=True),
        _tool("start_Cell-ID", _CDR_BLANK, fixed_name=True),
        _tool("start_CellBeam", _CDR_BLANK, fixed_name=True),
        _tool("end_Latitude", _CDR_BLANK, fixed_name=True),
        _tool("end_Longitude", _CDR_BLANK, fixed_name=True),
        _tool("end_Address", _CDR_BLANK, fixed_name=True),
        _tool("end_Cell-ID", _CDR_BLANK, fixed_name=True),
        _tool("end_CellBeam", _CDR_BLANK, fixed_name=True),
        _tool("DateTime offset", _OFFSET_MEANING.replace("TimeStamp", "DateTime"), field="timestamp_offset"),
        _tool("B_basis", "Where B_MSISDN comes from: same values as To basis.", field="to_basis"),
        Column("Source", "The communication's Source field, verbatim.", field="source"),
        *PROVENANCE_COLUMNS,
    ),
    (
        "The same communications as the Communications sheet, in the legacy CDR import layout.",
        "Identifiers are verbatim: unlike the legacy tool, the leading + is kept and blank A/B "
        "numbers are NOT filled with a target id.",
        "The columns after end_CellBeam (offset, basis, provenance) can be ignored by the importer.",
    ),
)

# Workbook order of the data sheets (About and Documentation come first).
SHEET_DOCS = (
    SOURCE_DEVICE, COMMUNICATIONS, MESSAGES, CALLS, EMAILS, CONTACTS, LOCATIONS, IDENTIFIERS, CDR_IMPORTER,
)
