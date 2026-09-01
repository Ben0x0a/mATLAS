"""The two data containers the whole library hands back: ``Container`` and ``Finding``.

Defines:    ``Container`` (one storage location an app owns, labelled with its category
            and — on Android — the user it belongs to) and ``Finding`` (something worth
            reporting that was observed while indexing or resolving).
Used by:    ``mobile_containers.ios`` and ``mobile_containers.android`` (they build
            them) and every caller of ``resolve``.
Depends on: the standard library only.

The two types live apart from the resolvers so a caller can import the result shape
without pulling in either platform's parsing machinery, and so the shared contract is
readable in one short file.

``Finding`` exists because this library must never resolve an oddity silently: an
identifier that maps to several containers of one category, a container directory with
no metadata plist, an unrecognised content class. Each is recorded here AND logged, so a
run can be audited after the fact rather than trusted.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Container:
    """One storage location owned by a package, as a locator inside the acquisition.

    ``path`` is the logical path of the container ROOT (the directory holding the app's
    files), expressed exactly as the injected opener lists it — including whatever
    wrapper segment the acquisition tool added (``filesystem1/``, ``Dump/``). It is a
    locator, not an on-disk path: the acquisition is never extracted.

    ``identifier`` is what the location was resolved THROUGH — on iOS the
    ``MCMMetadataIdentifier`` of the container (the team-prefixed bundle id, the
    extension's id, or the ``group.…`` name), on Android the package name. Keeping it
    distinguishes an app's own data from its widget's or its shared group's when several
    containers come back at once.

    ``user_id`` is the Android multi-user id the location belongs to and is ``None`` on
    iOS, which has no such dimension.
    """

    path: str
    category: str
    identifier: str
    user_id: int | None = None

    @property
    def sort_key(self) -> tuple[str, str, int, str]:
        """Total order that makes every ``resolve`` result deterministic.

        HOW: category, then identifier, then user id (``-1`` for iOS, which has none), then
        the path as the final tie-breaker so no two distinct containers can compare equal.
        WHY a property rather than a ``sort(key=...)`` lambda at each call site: both
        platforms' resolvers must emit the SAME order, and a single definition is the only
        way to guarantee that as the library grows."""
        return (
            self.category,
            self.identifier,
            -1 if self.user_id is None else self.user_id,
            self.path,
        )


@dataclass(frozen=True)
class Finding:
    """Something observed while indexing or resolving that a caller must be told about.

    ``kind`` is a stable machine-readable tag (see the ``*_FINDING_NAME`` constants in
    ``mobile_containers.categories``); ``detail`` is the human-readable specifics.
    """

    kind: str
    detail: str
