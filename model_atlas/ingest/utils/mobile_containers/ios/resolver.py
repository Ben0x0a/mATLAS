"""The iOS answer to "where does this app keep its data": ``IosResolver.resolve``.

Defines:    ``IosResolver`` — builds the index once from an ``Opener``, then answers
            ``resolve(package, category=None) -> list[Container]``.
Used by:    ``mobile_containers`` (re-exported) and, later, the mATLAS preset matcher.
Depends on: ``mobile_containers.ios.metadata`` / ``.references`` / ``.identifiers``,
            ``mobile_containers.models``, ``mobile_containers.openers`` (type only).

The two sources are joined here, and only here:

* ``metadata`` says which GUID container belongs to which identifier and what kind it is;
* ``references`` says which app groups an app declares and which extensions are its children.

Resolution is therefore a three-way fan-out from one package name — its own containers,
its children's containers, its groups' containers — and every branch ends in the same
identifier lookup against the metadata index. That is why an app group container comes
back with the group's name as its ``identifier``: it is a shared location the app reaches,
not a location named after the app.

WHY the index is built in ``__init__`` rather than per call: opening an acquisition archive
costs seconds and answering a lookup from a built index costs microseconds, so a resolver
is constructed once per acquisition and queried as often as wanted.
"""
from __future__ import annotations

import logging

from ..models import Container, Finding
from ..openers import Opener
from .identifiers import identifiers_match
from .metadata import MetadataIndex, build_metadata_index
from .references import AppRecord, ReferencesIndex, read_references

log = logging.getLogger(__name__)


class IosResolver:
    """Every storage location an iOS package owns, resolved from one acquisition."""

    def __init__(self, opener: Opener) -> None:
        self.metadata: MetadataIndex = build_metadata_index(opener)
        self.references: ReferencesIndex = read_references(opener)

    @property
    def findings(self) -> tuple[Finding, ...]:
        """Everything worth reporting from building the index, in a stable order."""
        return self.metadata.findings + self.references.findings

    def resolve(self, package: str, category: str | None = None) -> list[Container]:
        """The containers ``package`` owns, optionally narrowed to one ``category``.

        Without a category every location comes back, each labelled with its own category;
        with one, the list is narrowed to that kind. ``package`` may be written with or
        without its team-id prefix — see ``ios.identifiers`` for the matching rule.

        The result is deduplicated by path (an extension that also names the parent's group
        must not yield that group twice) and totally ordered, so the same acquisition always
        answers the same way.
        """
        found: dict[str, Container] = {}
        for container in self.metadata.lookup(package):
            found[container.path] = container

        for record in self._records_for(package):
            for child in self.references.children_by_parent.get(record.row_id, ()):
                for container in self.metadata.lookup(child):
                    found[container.path] = container
            # HOW: an extension may declare a group its parent does not, so the groups of
            # the app AND of each of its children are followed. WHY: leaving a child's group
            # out would silently lose a shared location the app's data really reaches.
            for group in self._group_names(record):
                for container in self.metadata.lookup(group):
                    found[container.path] = container

        containers = sorted(found.values(), key=lambda c: c.sort_key)
        if category is not None:
            containers = [c for c in containers if c.category == category]
        log.info(
            f"Resolved {package!r}"
            + (f" category {category!r}" if category is not None else "")
            + f" to {len(containers)} container(s)"
        )
        return containers

    def _records_for(self, package: str) -> list[AppRecord]:
        """Every ``code_signing_info`` row naming this package."""
        return [r for r in self.references.records if identifiers_match(r.identifier, package)]

    def _group_names(self, record: AppRecord) -> list[str]:
        """The app groups declared by ``record`` and by each of its child bundles."""
        names = list(record.app_groups)
        children = set(self.references.children_by_parent.get(record.row_id, ()))
        for other in self.references.records:
            if other.identifier in children:
                names.extend(other.app_groups)
        return names
