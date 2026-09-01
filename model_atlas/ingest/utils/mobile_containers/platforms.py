"""One entry point for both platforms: ``detect_platform`` and ``build_resolver``.

Defines:    the ``Resolver`` protocol both platform resolvers satisfy, the
            ``IOS_PLATFORM_NAME`` / ``ANDROID_PLATFORM_NAME`` tags,
            ``UnsupportedAcquisitionError``, ``detect_platform`` and ``build_resolver``.
Used by:    ``mobile_containers`` (re-exported) and any caller that has an acquisition
            without knowing which platform produced it.
Depends on: ``mobile_containers.android.resolver`` / ``.ios.metadata`` (their existing
            detectors), ``.models``, ``.openers`` (type only) and the standard library.

WHY the dispatch lives HERE and not in the calling application: "which platform is this
acquisition?" is a question about the acquisition, answerable only from the same evidence
the resolvers already read. Putting it in the caller would duplicate the recognition rules
outside the library and leave a detached copy of it unable to answer its own question.

WHY it reuses the resolvers' own detectors rather than adding markers of its own: the house
rule is one source of truth for recognition logic. iOS is recognised by the presence of the
container-manager metadata plists — exactly what ``build_metadata_index`` keys on — and
Android by the ``data/{data,user,user_de,media}`` tree ``detect_root_depth`` already finds.
A second, parallel set of markers here could drift from the ones that do the work.
"""
from __future__ import annotations

import logging
from typing import Protocol

from .android.resolver import AndroidResolver, detect_root_depth
from .ios.metadata import METADATA_PLIST_NAME
from .ios.resolver import IosResolver
from .models import Container, Finding
from .openers import Opener

log = logging.getLogger(__name__)

IOS_PLATFORM_NAME = "ios"
ANDROID_PLATFORM_NAME = "android"


class UnsupportedAcquisitionError(Exception):
    """Raised when an acquisition matches neither platform's layout.

    An exception rather than a ``None`` return because ``build_resolver`` promises a usable
    resolver: there is no meaningful resolver for an unrecognised layout, and handing back a
    silently empty one would report "this app owns nothing" for every query. A caller that
    wants to ASK rather than assume calls ``detect_platform``, which answers ``None``.
    """


class Resolver(Protocol):
    """What both platform resolvers offer — the contract a caller can rely on.

    Deliberately the whole surface: a caller holding a ``Resolver`` never needs to know
    which platform produced it.
    """

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Everything worth reporting from building the index, in a stable order."""

    def resolve(self, package: str, category: str | None = None) -> list[Container]:
        """The locations ``package`` owns, optionally narrowed to one ``category``."""


def detect_platform(opener: Opener) -> str | None:
    """Which platform produced this acquisition, or ``None`` when neither layout is found.

    HOW: one pass over the logical paths, stopping at the first decisive marker — an iOS
    container-manager metadata plist, or the Android data tree. WHY iOS is tested first:
    its marker is an exact filename match, so it is both cheaper and less likely to be
    coincidental than a directory-shape match.

    A ``None`` answer is an ordinary outcome (someone pointed the library at a report
    bundle, a backup, or the wrong zip), not an error — see ``UnsupportedAcquisitionError``.
    """
    paths = list(opener.list_paths())
    suffix = "/" + METADATA_PLIST_NAME
    if any(path.endswith(suffix) for path in paths):
        return IOS_PLATFORM_NAME
    if detect_root_depth(paths) is not None:
        return ANDROID_PLATFORM_NAME
    return None


def build_resolver(opener: Opener) -> Resolver:
    """The resolver for whichever platform this acquisition came from.

    The one call a caller needs: hand it an opened acquisition and ask it for an app's
    storage locations, without branching on the platform yourself.

    Raises ``UnsupportedAcquisitionError`` when neither layout is present.
    """
    platform = detect_platform(opener)
    if platform == IOS_PLATFORM_NAME:
        log.info("Acquisition detected as iOS; building the container index")
        return IosResolver(opener)
    if platform == ANDROID_PLATFORM_NAME:
        log.info("Acquisition detected as Android; building the container index")
        return AndroidResolver(opener)
    raise UnsupportedAcquisitionError(
        "this acquisition carries neither an iOS container-manager metadata plist nor an "
        "Android /data tree, so no container resolver applies to it"
    )
