"""Source 2 of 2: MobileContainerManager's ``references.sqlite3``, read WAL-aware.

Defines:    ``REFERENCES_DB_SUFFIX``, ``AppRecord`` (one signed bundle: its identifier,
            data-container class, app groups and keychain groups), ``ReferencesIndex``
            (records + parent -> child bundles + findings) and ``read_references``.
Used by:    ``mobile_containers.ios.resolver``, which joins it against
            ``mobile_containers.ios.metadata``.
Depends on: ``mobile_containers.models`` / ``.categories``, ``mobile_containers.openers``
            (type only) and the standard library (``sqlite3``, ``plistlib``).

The metadata plists say which container belongs to which identifier, but not that an app
also owns a *shared group* container, nor that a widget's container belongs to its parent
app. Both facts live in this database:

* ``code_signing_info`` — one row per signed bundle on the device;
* ``code_signing_data`` — a **binary plist inside a BLOB**, the cached entitlements, holding
  ``com.apple.security.application-groups`` (the group container names) and
  ``keychain-access-groups`` (the ``agrp`` an app's protected items are filed under);
* ``child_bundles`` — extension/widget identifier -> parent app row.

WHY this database and not ``applicationState.db``: the other candidate yields the bundle and
data paths that the metadata plists already give, and no group at all — its blobs contain no
``group.`` string anywhere — behind an ``NSKeyedArchiver`` payload on a schema that shifts
across releases. One mechanism answering both questions beats two answering one and a half.

WHY it must be read WAL-aware: the acquisition carries a 2.6 MB main file beside a 1.3 MB
write-ahead log. Opening the main file alone — which is what ``immutable=1`` does, and why
it is tempting on read-only evidence — showed 1077 of 1088 ``code_signing_info`` rows and
313 of 317 ``child_bundles`` rows on the verified device. The eleven missing rows are the
most recently installed apps, i.e. exactly the ones an examiner is most likely to be asked
about. So the database and its sidecars are staged to a throwaway directory and opened
``mode=ro``, letting SQLite replay the log. The acquisition itself is never touched.
"""
from __future__ import annotations

import logging
import plistlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..categories import (
    MISSING_REFERENCES_FINDING_NAME,
    UNREADABLE_ENTITLEMENTS_FINDING_NAME,
)
from ..models import Finding
from ..openers import Opener

log = logging.getLogger(__name__)

REFERENCES_DB_SUFFIX = "private/var/mobile/Library/MobileContainerManager/references.sqlite3"
# The write-ahead log and its shared-memory index must be staged beside the main file or
# SQLite cannot replay the log — see the module docstring for what that would cost.
SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm")

ENTITLEMENTS_KEY = "com.apple.MobileContainerManager.Entitlements"
APP_GROUPS_KEY = "com.apple.security.application-groups"
KEYCHAIN_GROUPS_KEY = "keychain-access-groups"


@dataclass(frozen=True)
class AppRecord:
    """One signed bundle as MobileContainerManager recorded it."""

    row_id: int
    identifier: str
    data_container_class: int
    app_groups: tuple[str, ...] = ()
    # Kept because it comes free from the same blob and is the ``agrp`` value an app's
    # protected keychain items are filed under — the second question this one lookup
    # answers, and the reason no separate mechanism is needed for it.
    keychain_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferencesIndex:
    """Everything ``references.sqlite3`` contributes, or an empty index plus a finding."""

    records: tuple[AppRecord, ...] = ()
    # parent ``code_signing_info.id`` -> the identifiers of its extensions/widgets.
    children_by_parent: dict[int, tuple[str, ...]] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()


def read_references(opener: Opener) -> ReferencesIndex:
    """Stage ``references.sqlite3`` with its sidecars and read the three tables it holds.

    HOW: locate the database by path suffix (root-prefix agnostic, so the acquisition
    tool's wrapper segment does not matter), copy it and any ``-wal``/``-shm`` sibling into
    a temporary directory, open ``file:…?mode=ro`` so SQLite replays the log, read the
    tables, then delete the staging directory.

    WHY a copy: ``sqlite3`` cannot read from a stream, and log replay needs the sidecars
    beside the main file in a writable directory. Only these few megabytes are staged —
    the acquisition is never extracted wholesale and never modified.
    """
    paths = {path for path in opener.list_paths()}
    main_path = _find_database(paths)
    if main_path is None:
        detail = f"no path ending in {REFERENCES_DB_SUFFIX!r}"
        log.warning(
            f"MobileContainerManager references database not found ({detail}); app groups and "
            f"extension links cannot be resolved"
        )
        return ReferencesIndex(findings=(Finding(MISSING_REFERENCES_FINDING_NAME, detail),))

    staging = Path(tempfile.mkdtemp(prefix="mobile_containers_refs_"))
    try:
        staged = _stage(opener, paths, main_path, staging)
        return _read_staged(staged)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _find_database(paths: set[str]) -> str | None:
    """The acquisition path of ``references.sqlite3``, matched on a full-segment suffix."""
    candidates = sorted(
        path
        for path in paths
        if path == REFERENCES_DB_SUFFIX or path.endswith("/" + REFERENCES_DB_SUFFIX)
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        # Deterministic choice, and loud: two MobileContainerManager databases in one
        # acquisition means more than one filesystem was captured into it.
        log.warning(
            f"{len(candidates)} MobileContainerManager references databases present; "
            f"reading {candidates[0]}"
        )
    return candidates[0]


def _stage(opener: Opener, paths: set[str], main_path: str, staging: Path) -> Path:
    """Copy the database and its sidecars out of the acquisition into ``staging``."""
    destination = staging / "references.sqlite3"
    _copy_out(opener, main_path, destination)
    for suffix in SIDECAR_SUFFIXES:
        sidecar = main_path + suffix
        if sidecar in paths:
            _copy_out(opener, sidecar, destination.with_name(destination.name + suffix))
            log.info(f"Staged references sidecar {suffix} for WAL-aware read")
    return destination


def _copy_out(opener: Opener, path: str, destination: Path) -> None:
    with opener.open(path) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)


def _read_staged(database: Path) -> ReferencesIndex:
    """Read the three tables from the staged copy, replaying the write-ahead log."""
    findings: list[Finding] = []
    # mode=ro (never immutable=1): immutable promises SQLite the file cannot change, so it
    # skips the write-ahead log entirely and silently returns a stale view of the device.
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        entitlements = _read_entitlements(connection, findings)
        records = tuple(
            AppRecord(
                row_id=row_id,
                identifier=identifier,
                data_container_class=container_class,
                app_groups=entitlements.get(row_id, ((), ()))[0],
                keychain_groups=entitlements.get(row_id, ((), ()))[1],
            )
            for row_id, identifier, container_class in sorted(
                connection.execute(
                    "SELECT id, code_signing_id_text, data_container_class FROM code_signing_info"
                )
            )
        )
        children = _read_children(connection)
    finally:
        connection.close()
    log.info(
        f"Read {len(records)} code-signing records and "
        f"{sum(len(c) for c in children.values())} child bundles from references.sqlite3"
    )
    return ReferencesIndex(
        records=records, children_by_parent=children, findings=tuple(findings)
    )


def _read_entitlements(
    connection: sqlite3.Connection, findings: list[Finding]
) -> dict[int, tuple[tuple[str, ...], tuple[str, ...]]]:
    """``cs_info_id`` -> (app groups, keychain groups) from each cached-entitlement blob.

    HOW: every ``code_signing_data.data`` BLOB is a binary plist; the entitlements sit in a
    nested dictionary under ``ENTITLEMENTS_KEY``.
    WHY a blob that fails to parse is recorded rather than raised: one app's malformed
    cache must not cost every other app its group resolution.
    """
    parsed: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for cs_info_id, blob in sorted(connection.execute(
        "SELECT cs_info_id, data FROM code_signing_data"
    )):
        try:
            payload = plistlib.loads(bytes(blob))
        except Exception as error:  # noqa: BLE001 - one bad blob must not abort the read
            findings.append(
                Finding(UNREADABLE_ENTITLEMENTS_FINDING_NAME, f"cs_info_id={cs_info_id}: {error}")
            )
            log.warning(f"Unreadable cached entitlements for cs_info_id={cs_info_id}: {error}")
            continue
        entitlements = payload.get(ENTITLEMENTS_KEY) if isinstance(payload, dict) else None
        if not isinstance(entitlements, dict):
            continue
        parsed[cs_info_id] = (
            _string_tuple(entitlements.get(APP_GROUPS_KEY)),
            _string_tuple(entitlements.get(KEYCHAIN_GROUPS_KEY)),
        )
    return parsed


def _read_children(connection: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
    children: dict[int, list[str]] = {}
    for child, parent_id in sorted(
        connection.execute("SELECT child_code_signing_id_text, parent_id FROM child_bundles")
    ):
        children.setdefault(parent_id, []).append(child)
    return {parent: tuple(sorted(kids)) for parent, kids in children.items()}


def _string_tuple(value: object) -> tuple[str, ...]:
    """An entitlement value normalised to a tuple of strings.

    An entitlement may be absent, a single string, or a list; anything else is not a group
    name and is dropped rather than guessed at.
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str))
    return ()
