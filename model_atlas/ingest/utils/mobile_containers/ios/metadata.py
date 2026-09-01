"""Source 1 of 2: every container's own metadata plist, read in one streamed pass.

Defines:    ``METADATA_PLIST_NAME``, ``MetadataIndex`` (identifier -> containers, plus
            the findings raised while building it) and ``build_metadata_index``.
Used by:    ``mobile_containers.ios.resolver`` (joined against
            ``mobile_containers.ios.references`` to answer ``resolve``).
Depends on: ``mobile_containers.models`` / ``.categories`` / ``.ios.identifiers``,
            ``mobile_containers.openers`` (type only) and the standard library.

Every iOS container directory carries a tiny
``.com.apple.mobile_container_manager.metadata.plist`` naming the app that owns it
(``MCMMetadataIdentifier``) and the kind of container it is
(``MCMMetadataContentClass``). Its directory name is the per-install GUID that a preset
cannot hard-code. Reading all of them therefore inverts the problem: instead of guessing
a path, the whole device is indexed once, identifier by identifier, and a lookup is a
dictionary hit. Coverage on the verified acquisition was 1178/1178 containers.

WHY the container roots are derived from the plists rather than hard-coded: the seven
trees Apple uses (``Bundle/Application``, ``Data/Application``, ``Data/PluginKitPlugin``,
``Shared/AppGroup``, ``Data/InternalDaemon``, ``Data/System``, ``Shared/SystemGroup``)
sit under two different parents and are renamed across releases, and the acquisition tool
adds a wrapper segment of its own. Deriving them from the data keeps the reader correct
on a tree it has never seen, and makes "a container with no metadata plist" a computable
fact rather than an assumption.
"""
from __future__ import annotations

import logging
import plistlib
from dataclasses import dataclass

from ..categories import (
    AMBIGUOUS_IDENTIFIER_FINDING_NAME,
    MISSING_METADATA_FINDING_NAME,
    UNKNOWN_CONTENT_CLASS_FINDING_NAME,
    UNREADABLE_METADATA_FINDING_NAME,
    category_for_content_class,
)
from ..models import Container, Finding
from ..openers import Opener
from .identifiers import strip_team_prefix

log = logging.getLogger(__name__)

METADATA_PLIST_NAME = ".com.apple.mobile_container_manager.metadata.plist"
IDENTIFIER_KEY = "MCMMetadataIdentifier"
CONTENT_CLASS_KEY = "MCMMetadataContentClass"


@dataclass(frozen=True)
class MetadataIndex:
    """Every container found on the device, looked up by the identifier that owns it."""

    containers: tuple[Container, ...]
    findings: tuple[Finding, ...]
    # Keyed on the team-prefix-stripped identifier, so a caller holding either the bare
    # or the prefixed form hits the same entry — see ``ios.identifiers`` for why the
    # strip is shape-checked rather than a plain first-segment drop.
    _by_identifier: dict[str, tuple[Container, ...]]

    def lookup(self, identifier: str) -> tuple[Container, ...]:
        """Every container owned by ``identifier``, in the index's deterministic order."""
        return self._by_identifier.get(strip_team_prefix(identifier), ())


def build_metadata_index(opener: Opener) -> MetadataIndex:
    """Index every container on the device in one streamed pass over the acquisition.

    HOW: walk the logical paths once to collect the metadata plists, derive the container
    roots from them (the grandparent of each plist), walk once more to spot container
    directories under those roots that carry no plist, then read each plist through the
    single already-open handle the ``Opener`` holds.

    WHY two walks rather than one: the second needs the roots the first discovers, and a
    walk is a cheap in-memory replay of the archive's central directory — the expensive
    thing, opening the archive, has already happened. ``Opener.list_paths`` is therefore
    required to be re-iterable.

    WHY every plist is read even though a caller wants one app: opening the archive per
    lookup is what made the naive approach take ten minutes; one pass over 1178 plists
    through an open handle takes a fifth of a second and answers every later question.
    """
    findings: list[Finding] = []

    plist_paths = sorted(
        path for path in opener.list_paths() if path.endswith("/" + METADATA_PLIST_NAME)
    )
    # The container root is the plist's grandparent: <root>/<GUID>/<plist>.
    container_dirs = {path[: -(len(METADATA_PLIST_NAME) + 1)] for path in plist_paths}
    roots = {directory.rsplit("/", 1)[0] for directory in container_dirs}
    findings.extend(_missing_metadata_findings(opener, roots, container_dirs))

    containers: list[Container] = []
    for plist_path in plist_paths:
        container = _read_container(opener, plist_path, findings)
        if container is not None:
            containers.append(container)

    ordered = tuple(sorted(containers, key=lambda c: c.sort_key))
    by_identifier = _group_by_identifier(ordered)
    findings.extend(_ambiguity_findings(by_identifier))
    log.info(
        f"Indexed {len(ordered)} iOS containers from {len(plist_paths)} metadata plists "
        f"across {len(roots)} container roots ({len(findings)} finding(s))"
    )
    return MetadataIndex(containers=ordered, findings=tuple(findings), _by_identifier=by_identifier)


def _read_container(opener: Opener, plist_path: str, findings: list[Finding]) -> Container | None:
    """Turn one metadata plist into a ``Container``, or record why it could not be."""
    try:
        with opener.open(plist_path) as stream:
            metadata = plistlib.loads(stream.read())
    except Exception as error:  # noqa: BLE001 - a malformed plist must not abort the index
        # WHY not re-raised: one unreadable plist on a device holding a thousand of them
        # would otherwise cost every other app its resolution. It is recorded instead, so
        # the gap is visible rather than silent.
        findings.append(Finding(UNREADABLE_METADATA_FINDING_NAME, f"{plist_path}: {error}"))
        log.warning(f"Unreadable container metadata at {plist_path}: {error}")
        return None

    identifier = metadata.get(IDENTIFIER_KEY)
    content_class = metadata.get(CONTENT_CLASS_KEY)
    if not isinstance(identifier, str) or not isinstance(content_class, int):
        findings.append(
            Finding(
                UNREADABLE_METADATA_FINDING_NAME,
                f"{plist_path}: {IDENTIFIER_KEY}={identifier!r} {CONTENT_CLASS_KEY}={content_class!r}",
            )
        )
        log.warning(f"Container metadata at {plist_path} lacks a usable identifier or content class")
        return None

    category, known = category_for_content_class(content_class)
    if not known:
        # An unrecognised class means a container tree this library has not seen. It is
        # surfaced with a distinguishable category so the location still reaches the
        # caller — dropping it would hide storage the app really owns.
        findings.append(
            Finding(
                UNKNOWN_CONTENT_CLASS_FINDING_NAME,
                f"{plist_path}: {CONTENT_CLASS_KEY}={content_class} for {identifier!r}",
            )
        )
        log.warning(
            f"Unknown {CONTENT_CLASS_KEY} {content_class} for {identifier!r} at {plist_path}; "
            f"surfaced as category {category!r}"
        )
    return Container(
        path=plist_path[: -(len(METADATA_PLIST_NAME) + 1)],
        category=category,
        identifier=identifier,
    )


def _missing_metadata_findings(
    opener: Opener, roots: set[str], container_dirs: set[str]
) -> list[Finding]:
    """Report every container directory that carries no metadata plist.

    HOW: replay the path listing, keep the immediate child directory of each known
    container root, and subtract the directories a plist was found in.
    WHY it is reported rather than ignored: such a container is storage this library
    cannot attribute to an app, so counting it turns an unknown-unknown into a known gap.
    A silent skip would make the index look complete when it is not.
    """
    observed: set[str] = set()
    for path in opener.list_paths():
        for root in roots:
            prefix = root + "/"
            if not path.startswith(prefix):
                continue
            child = path[len(prefix):].split("/", 1)[0]
            if child:
                observed.add(f"{root}/{child}")
            break
    missing = sorted(observed - container_dirs)
    for directory in missing:
        log.warning(f"Container directory without a metadata plist: {directory}")
    if missing:
        log.warning(f"{len(missing)} container directory(ies) carry no metadata plist")
    return [Finding(MISSING_METADATA_FINDING_NAME, directory) for directory in missing]


def _group_by_identifier(containers: tuple[Container, ...]) -> dict[str, tuple[Container, ...]]:
    grouped: dict[str, list[Container]] = {}
    for container in containers:
        grouped.setdefault(strip_team_prefix(container.identifier), []).append(container)
    return {key: tuple(value) for key, value in grouped.items()}


def _ambiguity_findings(by_identifier: dict[str, tuple[Container, ...]]) -> list[Finding]:
    """Report an identifier owning several containers of one category.

    WHY reported and not resolved: two containers of the same category for one app means
    a stale install, a restore, or a migration left a second copy behind. Picking the
    first would silently discard evidence and the choice would not even be recorded, so
    the resolver returns BOTH and the ambiguity is written down here instead.
    """
    findings: list[Finding] = []
    for identifier, containers in sorted(by_identifier.items()):
        per_category: dict[str, list[str]] = {}
        for container in containers:
            per_category.setdefault(container.category, []).append(container.path)
        for category, paths in sorted(per_category.items()):
            if len(paths) < 2:
                continue
            detail = f"{identifier!r} category {category!r}: {', '.join(sorted(paths))}"
            findings.append(Finding(AMBIGUOUS_IDENTIFIER_FINDING_NAME, detail))
            log.warning(f"Ambiguous container set — {detail}")
    return findings
