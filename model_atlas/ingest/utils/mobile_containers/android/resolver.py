"""The Android answer to "where does this package keep its data": ``AndroidResolver``.

Defines:    ``AndroidResolver`` — indexes the acquisition once, then answers
            ``resolve(package, category=None) -> list[Container]``.
Used by:    ``mobile_containers`` (re-exported).
Depends on: ``mobile_containers.categories`` / ``.models``, ``mobile_containers.openers``
            (type only) and the standard library.

Android needs no GUID join: its paths are stable and a package name IS the directory name.
What it has instead — and iOS does not — is **multiple users**. The verified Pixel
acquisition carries two (0 and 10), each with its own credential-encrypted and
device-encrypted trees, so a package's data is spread across up to six locations:

    /data/data/<pkg>                              credential-encrypted, user 0
    /data/user/<uid>/<pkg>                        credential-encrypted, per user
    /data/user_de/<uid>/<pkg>                     device-encrypted, per user
    /data/media/<uid>/Android/{data,media,obb}/<pkg>   external storage, per user

Every location is returned labelled with its kind and its user id. On that device Snapchat
occupies two of them (``data/data`` and ``data/user_de/0``) while Chrome occupies six, and
``/data/user/0`` exists but is empty because ``/data/data`` IS user 0's tree.

WHY the root prefix is detected rather than configured: acquisition tools wrap the
filesystem in a directory of their own (``Dump/`` here, ``filesystem1/`` on the iOS side),
and a caller should not have to know its name. WHY it is detected as a fixed depth and then
matched strictly at that depth: a package's own cache can contain a path that looks like
another layout (``…/com.snapchat.android/cache/data/user/0/com.snapchat.android`` is real on
this device), and a pattern allowed to match anywhere in a path would report that cache
directory as a second user-0 container.
"""
from __future__ import annotations

import logging

from ..categories import (
    ANDROID_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME,
    ANDROID_EXTERNAL_OBB_CATEGORY_NAME,
    ANDROID_USER_CATEGORY_NAME,
    ANDROID_USER_DE_CATEGORY_NAME,
)
from ..models import Container
from ..openers import Opener

log = logging.getLogger(__name__)

# The second segment of every layout above; used to recognise the ``data/`` root.
_DATA_CHILD_SEGMENTS = frozenset({"data", "user", "user_de", "media"})
# ``/data/media/<uid>/Android/<this>/<pkg>`` -> category.
_EXTERNAL_CATEGORIES: dict[str, str] = {
    "data": ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
    "media": ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME,
    "obb": ANDROID_EXTERNAL_OBB_CATEGORY_NAME,
}
# ``/data/data`` is the canonical name of user 0's credential-encrypted tree — it is a bind
# of ``/data/user/0`` — so a container found there belongs to user 0, not to "no user".
_PRIMARY_USER_ID = 0


class AndroidResolver:
    """Every storage location an Android package owns, resolved from one acquisition."""

    def __init__(self, opener: Opener) -> None:
        self._by_package: dict[str, list[Container]] = {}
        paths = list(opener.list_paths())
        self.root_depth = _detect_root_depth(paths)
        if self.root_depth is None:
            log.warning("No Android data tree found in this acquisition; nothing to index")
            return
        self._index(paths, self.root_depth)
        log.info(
            f"Indexed {sum(len(v) for v in self._by_package.values())} Android container(s) "
            f"for {len(self._by_package)} package(s) at root depth {self.root_depth}"
        )

    def resolve(self, package: str, category: str | None = None) -> list[Container]:
        """The locations ``package`` owns, optionally narrowed to one ``category``.

        Every location is labelled with its kind and the user id it belongs to; the order
        is total (see ``Container.sort_key``), so the same acquisition answers the same way.
        """
        containers = sorted(self._by_package.get(package, ()), key=lambda c: c.sort_key)
        if category is not None:
            containers = [c for c in containers if c.category == category]
        log.info(
            f"Resolved {package!r}"
            + (f" category {category!r}" if category is not None else "")
            + f" to {len(containers)} location(s)"
        )
        return containers

    def _index(self, paths: list[str], root_depth: int) -> None:
        """Record one container per (package, layout, user) seen at the detected depth.

        HOW: each path is split once and read against the layouts at the fixed root depth;
        a hit contributes its container ROOT path, deduplicated because every file under a
        container yields the same root.
        """
        seen: set[str] = set()
        for path in paths:
            segments = path.split("/")
            container = _match_layout(segments, root_depth)
            if container is None or container.path in seen:
                continue
            seen.add(container.path)
            self._by_package.setdefault(container.identifier, []).append(container)


def _detect_root_depth(paths: list[str]) -> int | None:
    """The number of wrapper segments the acquisition tool put before ``data/``.

    HOW: the shallowest position at which a ``data/`` segment is followed by one of the
    four known children. WHY the shallowest: any deeper occurrence is inside some app's own
    files, and taking the minimum over the whole listing makes the choice independent of
    the order paths arrive in.
    """
    shallowest: int | None = None
    for path in paths:
        segments = path.split("/")
        for index in range(len(segments) - 1):
            if shallowest is not None and index >= shallowest:
                break
            if segments[index] == "data" and segments[index + 1] in _DATA_CHILD_SEGMENTS:
                shallowest = index
                break
    return shallowest


def _match_layout(segments: list[str], root_depth: int) -> Container | None:
    """The container a path denotes, or ``None`` when it denotes none.

    Every branch is anchored at ``root_depth`` — the guard that stops an app's own cache
    directory from being read as a second container (see the module docstring).
    """
    if len(segments) <= root_depth + 2 or segments[root_depth] != "data":
        return None
    tree = segments[root_depth + 1]

    if tree == "data":
        package = segments[root_depth + 2]
        return _container(segments, root_depth + 3, ANDROID_DATA_CATEGORY_NAME, package,
                          _PRIMARY_USER_ID)

    if tree in ("user", "user_de"):
        if len(segments) <= root_depth + 3:
            return None
        user_id = _user_id(segments[root_depth + 2])
        if user_id is None:
            return None
        category = (
            ANDROID_USER_CATEGORY_NAME if tree == "user" else ANDROID_USER_DE_CATEGORY_NAME
        )
        return _container(segments, root_depth + 4, category, segments[root_depth + 3], user_id)

    if tree == "media":
        if len(segments) <= root_depth + 5 or segments[root_depth + 3] != "Android":
            return None
        user_id = _user_id(segments[root_depth + 2])
        category = _EXTERNAL_CATEGORIES.get(segments[root_depth + 4])
        if user_id is None or category is None:
            return None
        return _container(segments, root_depth + 6, category, segments[root_depth + 5], user_id)

    return None


def _container(
    segments: list[str], depth: int, category: str, package: str, user_id: int
) -> Container | None:
    """Build the container rooted at ``segments[:depth]``, or ``None`` for a bare leaf.

    A path that stops exactly AT the package segment is a file named like a package, not a
    container directory (``…/Android/data/.nomedia`` is the real case), so it is rejected —
    a container is only proven by something existing inside it.
    """
    if len(segments) <= depth or not package:
        return None
    return Container(
        path="/".join(segments[:depth]), category=category, identifier=package, user_id=user_id
    )


def _user_id(segment: str) -> int | None:
    """The Android multi-user id, or ``None`` when the segment is not one."""
    if not segment.isdigit():
        return None
    return int(segment)
