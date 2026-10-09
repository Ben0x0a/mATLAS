"""Operator-tunable settings of the combined workbook.

Defines:    CONTACT_PHONE_COLNAME, CONTACT_EMAIL_COLNAME, CONTACT_OTHER_IDS_COLNAME and
            CONTACT_APP_COLUMNS (the per-application identifier columns of the Contacts sheet);
            LOCATION_RECORD_TYPES (which records the Locations sheet lists).
Used by:    workbook.sheets (contacts, locations), workbook.documentation.
Depends on: nothing.

Edit CONTACT_APP_COLUMNS to add, remove or reorder an application column. Each entry is
(column header, regular expression). An identifier (a contact's UserID entry) goes to the FIRST
column whose expression matches, as a whole word and case-insensitively, its Category (e.g.
"Instagram Id"); failing that, the contact's Source (e.g. "Snapchat"). An identifier matching
nothing goes to CONTACT_OTHER_IDS_COLNAME. Every column is present even when empty, so the
layout is the same from one report to the next.
"""
from __future__ import annotations

CONTACT_PHONE_COLNAME = "Phone numbers"
CONTACT_EMAIL_COLNAME = "Email addresses"
CONTACT_OTHER_IDS_COLNAME = "Other identifiers"

CONTACT_APP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("WhatsApp", r"whats\s?app"),
    ("Facebook", r"facebook|messenger"),
    ("Instagram", r"instagram"),
    ("Snapchat", r"snapchat"),
    ("Telegram", r"telegram"),
    ("Signal", r"signal"),
    ("Threema", r"threema"),
    ("Viber", r"viber"),
    ("Discord", r"discord"),
    ("TikTok", r"tik\s?tok"),
    ("X (Twitter)", r"twitter|x"),
    ("Skype", r"skype"),
    ("LinkedIn", r"linked\s?in"),
    ("WeChat", r"we\s?chat"),
)

# Locations sheet. Records of these top-level types are listed, and so is any other type the
# report gives a Coordinate (Position), so no position is ever left out. "Journey" is listed
# per point (its WayPoints). Order = the order the rows appear in (no sorting).
LOCATION_RECORD_TYPES: tuple[str, ...] = (
    "Location",
    "CellTower",
    "WirelessNetwork",
    "Journey",
    "SearchedItem",
)
