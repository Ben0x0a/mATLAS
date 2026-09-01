"""Synthetic acquisitions for the ``mobile_containers`` tests — no evidence in the repo.

Defines:    ``build_references_db`` (a MobileContainerManager database with real cached
            entitlement blobs), the ``ios_acquisition`` / ``android_acquisition`` fixtures
            (tiny zips shaped exactly like the real trees) and ``REAL_*`` drive paths with
            ``requires_*`` skip markers for the tests that need an actual acquisition.
Used by:    every test module in ``tests/ingest/mobile_containers/``.
Depends on: the standard library and ``pytest`` only — deliberately NOT
            ``model_atlas.ingest.utils.mobile_containers``, so a fixture can never be
            built out of the code it is meant to check.

The committed fixtures are synthetic on purpose: a forensic repository must not carry real
acquisition data. They reproduce the *shapes* verified on two real devices — the GUID
container trees and the cached-entitlement blob on iOS, the six per-user layouts on
Android — including the awkward ones (a container with no metadata plist, an unrecognised
content class, one identifier owning two containers of a category, an app cache whose own
path imitates another layout). The two tests that must touch a real acquisition skip
cleanly when the external drive is not mounted.
"""
from __future__ import annotations

import plistlib
import sqlite3
import zipfile
from pathlib import Path

import pytest

# --- Real acquisitions (external drive; absent by default) ----------------
REAL_DRIVE = Path("/Volumes/Ben10_1TB")
REAL_IOS_ZIP = REAL_DRIVE / (
    "TM/2026_Théophile/2026-06-24/FFS/UFED Apple iPhone 13 Pro (D63AP) 2026_06_24 (001)/"
    "Manual/EXTRACTION_FFS 01/EXTRACTION_FFS.zip"
)
REAL_ANDROID_ZIP = REAL_DRIVE / "Dataset test/ScooBy/Google Pixel 9a/EXTRACTION_FFS.zip"

requires_real_ios = pytest.mark.skipif(
    not REAL_IOS_ZIP.exists(), reason="acquisition A not mounted"
)
requires_real_android = pytest.mark.skipif(
    not REAL_ANDROID_ZIP.exists(), reason="acquisition C not mounted"
)

# --- Synthetic iOS acquisition -------------------------------------------
IOS_ROOT = "fs1"
METADATA_PLIST_NAME = ".com.apple.mobile_container_manager.metadata.plist"
REFERENCES_PATH = f"{IOS_ROOT}/private/var/mobile/Library/MobileContainerManager/references.sqlite3"

TEAM_ID = "ABCDEFGHIJ"
APP_ID = f"{TEAM_ID}.com.example.app"
WIDGET_ID = f"{APP_ID}.widget"
GROUP_ID = "group.com.example"

BUNDLE_GUID = "11111111-1111-1111-1111-111111111111"
DATA_GUID = "22222222-2222-2222-2222-222222222222"
WIDGET_GUID = "33333333-3333-3333-3333-333333333333"
GROUP_GUID = "44444444-4444-4444-4444-444444444444"
UNKNOWN_CLASS_GUID = "55555555-5555-5555-5555-555555555555"
ORPHAN_GUID = "66666666-6666-6666-6666-666666666666"
DUPLICATE_GUIDS = (
    "77777777-7777-7777-7777-777777777777",
    "88888888-8888-8888-8888-888888888888",
)

BUNDLE_TREE = f"{IOS_ROOT}/private/var/containers/Bundle/Application"
DATA_TREE = f"{IOS_ROOT}/private/var/mobile/Containers/Data/Application"
PLUGIN_TREE = f"{IOS_ROOT}/private/var/mobile/Containers/Data/PluginKitPlugin"
GROUP_TREE = f"{IOS_ROOT}/private/var/mobile/Containers/Shared/AppGroup"

# An unmapped MCMMetadataContentClass, so the "surfaced, never dropped" path is exercised
# without pinning the test to a class Apple might one day actually define.
UNKNOWN_CONTENT_CLASS = 99


def _metadata_plist(identifier: str, content_class: int) -> bytes:
    return plistlib.dumps(
        {
            "MCMMetadataIdentifier": identifier,
            "MCMMetadataContentClass": content_class,
            "MCMMetadataSchemaVersion": 2,
        }
    )


def build_references_db(
    path: Path,
    *,
    apps: dict[int, tuple[str, int]],
    entitlements: dict[int, dict[str, list[str]]],
    children: dict[str, int],
) -> None:
    """Write a MobileContainerManager ``references.sqlite3`` with the three real tables.

    ``apps`` maps row id -> (code-signing identifier, data container class); ``entitlements``
    maps row id -> the entitlement dictionary cached for it; ``children`` maps an extension
    identifier -> its parent's row id. The entitlement dictionary is stored the way the
    device stores it: a binary plist inside a BLOB, nested under the MobileContainerManager
    entitlements key.
    """
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE code_signing_info(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "code_signing_id_text TEXT NOT NULL UNIQUE,invalid INTEGER NOT NULL DEFAULT 0,"
            "placeholder INTEGER NOT NULL DEFAULT 0,registered_by_caller INTEGER NOT NULL "
            "DEFAULT 0,data_container_class INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "CREATE TABLE code_signing_data(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "cs_info_id INTEGER NOT NULL UNIQUE,data BLOB NOT NULL,"
            "FOREIGN KEY(cs_info_id) REFERENCES code_signing_info(id) ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE TABLE child_bundles(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "child_code_signing_id_text TEXT NOT NULL UNIQUE,parent_id INTEGER NOT NULL,"
            "FOREIGN KEY(parent_id) REFERENCES code_signing_info(id) ON DELETE CASCADE)"
        )
        for row_id, (identifier, container_class) in sorted(apps.items()):
            connection.execute(
                "INSERT INTO code_signing_info(id, code_signing_id_text, data_container_class) "
                "VALUES (?,?,?)",
                (row_id, identifier, container_class),
            )
            blob = plistlib.dumps(
                {
                    "CodeInfoIdentifier": identifier,
                    "TeamIdentifier": TEAM_ID,
                    "com.apple.MobileContainerManager.Entitlements": entitlements.get(row_id, {}),
                },
                fmt=plistlib.FMT_BINARY,
            )
            connection.execute(
                "INSERT INTO code_signing_data(cs_info_id, data) VALUES (?,?)", (row_id, blob)
            )
        for child, parent_id in sorted(children.items()):
            connection.execute(
                "INSERT INTO child_bundles(child_code_signing_id_text, parent_id) VALUES (?,?)",
                (child, parent_id),
            )
    connection.close()


@pytest.fixture
def ios_acquisition(tmp_path: Path) -> Path:
    """A tiny zip shaped like an iOS full-filesystem acquisition.

    It carries the four containers a real app owns (bundle, data, widget, shared group)
    plus the three awkward cases the invariants are about: an unrecognised content class,
    a container directory with no metadata plist, and one identifier owning two data
    containers.
    """
    database = tmp_path / "references.sqlite3"
    build_references_db(
        database,
        apps={
            1: (APP_ID, 2),
            2: (WIDGET_ID, 4),
            3: ("com.dup.app", 2),
        },
        entitlements={
            1: {
                "com.apple.security.application-groups": [GROUP_ID],
                "keychain-access-groups": [f"{TEAM_ID}.{APP_ID}"],
            },
            2: {"com.apple.security.application-groups": [GROUP_ID]},
        },
        children={WIDGET_ID: 1},
    )

    archive = tmp_path / "ios.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for tree, guid, identifier, content_class in (
            (BUNDLE_TREE, BUNDLE_GUID, APP_ID, 1),
            (DATA_TREE, DATA_GUID, APP_ID, 2),
            (PLUGIN_TREE, WIDGET_GUID, WIDGET_ID, 4),
            (GROUP_TREE, GROUP_GUID, GROUP_ID, 7),
            (DATA_TREE, UNKNOWN_CLASS_GUID, "com.example.exotic", UNKNOWN_CONTENT_CLASS),
            (DATA_TREE, DUPLICATE_GUIDS[0], "com.dup.app", 2),
            (DATA_TREE, DUPLICATE_GUIDS[1], "com.dup.app", 2),
        ):
            zf.writestr(
                f"{tree}/{guid}/{METADATA_PLIST_NAME}",
                _metadata_plist(identifier, content_class),
            )
            zf.writestr(f"{tree}/{guid}/Documents/payload.txt", b"x")
        # A container directory carrying files but no metadata plist.
        zf.writestr(f"{DATA_TREE}/{ORPHAN_GUID}/Documents/payload.txt", b"x")
        zf.write(database, REFERENCES_PATH)
    return archive


# --- Synthetic Android acquisition ---------------------------------------
ANDROID_ROOT = "Dump"
ANDROID_PACKAGE = "com.example.app"


@pytest.fixture
def android_acquisition(tmp_path: Path) -> Path:
    """A tiny zip shaped like an Android full-filesystem acquisition.

    It reproduces both user ids seen on the real device, all six layouts, an empty
    ``/data/user/0`` (user 0's tree really is ``/data/data``), a ``.nomedia`` file sitting
    where a package name would be, and an app cache whose own path repeats the
    ``data/user/0/<pkg>`` shape — the trap that a non-anchored pattern falls into.
    """
    archive = tmp_path / "android.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in (
            f"{ANDROID_ROOT}/data/data/{ANDROID_PACKAGE}/databases/main.db",
            f"{ANDROID_ROOT}/data/data/{ANDROID_PACKAGE}/cache/data/user/0/{ANDROID_PACKAGE}/trap",
            f"{ANDROID_ROOT}/data/user/10/{ANDROID_PACKAGE}/databases/main.db",
            f"{ANDROID_ROOT}/data/user_de/0/{ANDROID_PACKAGE}/files/f",
            f"{ANDROID_ROOT}/data/user_de/10/{ANDROID_PACKAGE}/files/f",
            f"{ANDROID_ROOT}/data/media/0/Android/data/{ANDROID_PACKAGE}/cache/c",
            f"{ANDROID_ROOT}/data/media/0/Android/media/{ANDROID_PACKAGE}/m",
            f"{ANDROID_ROOT}/data/media/0/Android/obb/{ANDROID_PACKAGE}/o",
            f"{ANDROID_ROOT}/data/media/0/Android/data/.nomedia",
            f"{ANDROID_ROOT}/data/data/com.other.app/files/f",
            f"{ANDROID_ROOT}/data/misc/profiles/ref/{ANDROID_PACKAGE}",
            f"{ANDROID_ROOT}/data/app/~~abc==/{ANDROID_PACKAGE}-xyz==/base.apk",
        ):
            zf.writestr(path, b"x")
    return archive
