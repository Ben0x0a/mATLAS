"""iOS resolution over a synthetic acquisition: the join, the matching rule, the findings.

Defines:    tests for ``strip_team_prefix`` / ``identifiers_match``, for the metadata
            index (unknown content class, container without a plist, ambiguity) and for
            ``IosResolver.resolve`` with and without a category.
Used by:    the test suite.
Depends on: the ``ios_acquisition`` fixture in this directory's ``conftest`` and the
            public surface of ``model_atlas.ingest.utils.mobile_containers``.

Every case here is a shape verified on a real device, rebuilt at a size the repository can
hold — see the conftest docstring for why no real acquisition data is committed.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.ingest.utils.mobile_containers import (
    IOS_APP_GROUP_CATEGORY_NAME,
    IOS_BUNDLE_CATEGORY_NAME,
    IOS_DATA_CATEGORY_NAME,
    IOS_EXTENSION_CATEGORY_NAME,
    IosResolver,
    ZipOpener,
)
from model_atlas.ingest.utils.mobile_containers.categories import (
    AMBIGUOUS_IDENTIFIER_FINDING_NAME,
    MISSING_METADATA_FINDING_NAME,
    UNKNOWN_CATEGORY_PREFIX,
    UNKNOWN_CONTENT_CLASS_FINDING_NAME,
)
from model_atlas.ingest.utils.mobile_containers.ios.identifiers import (
    identifiers_match,
    strip_team_prefix,
)
from tests.ingest.mobile_containers.conftest import (
    APP_ID,
    BUNDLE_GUID,
    DATA_GUID,
    DUPLICATE_GUIDS,
    GROUP_GUID,
    GROUP_ID,
    ORPHAN_GUID,
    UNKNOWN_CLASS_GUID,
    UNKNOWN_CONTENT_CLASS,
    WIDGET_GUID,
    WIDGET_ID,
)


def _resolver(archive: Path) -> IosResolver:
    with ZipOpener(archive) as opener:
        return IosResolver(opener)


# --- The matching rule ----------------------------------------------------

def test_team_prefix_is_stripped_only_when_it_looks_like_a_team_id():
    assert strip_team_prefix("5Q4J53EFRC.com.sbb.ch") == "com.sbb.ch"
    # "com" is not ten upper-case alphanumerics, so nothing is stripped — otherwise
    # "sbb.ch" would match "com.sbb.ch" and one app's data would be filed under another.
    assert strip_team_prefix("com.sbb.ch") == "com.sbb.ch"
    assert strip_team_prefix("com.toyopagroup.picaboo") == "com.toyopagroup.picaboo"


def test_identifier_matching_is_symmetric_and_not_a_substring_match():
    assert identifiers_match("com.sbb.ch", "5Q4J53EFRC.com.sbb.ch")
    assert identifiers_match("5Q4J53EFRC.com.sbb.ch", "com.sbb.ch")
    # An extension is a different bundle, never the app itself.
    assert not identifiers_match("com.sbb.ch", "5Q4J53EFRC.com.sbb.ch.tripsandticketswidget")
    assert not identifiers_match("sbb.ch", "com.sbb.ch")


# --- Resolution -----------------------------------------------------------

def test_resolve_returns_every_container_the_app_owns(ios_acquisition: Path):
    """A bare bundle id resolves through the team-prefixed identifier to all four kinds."""
    containers = _resolver(ios_acquisition).resolve("com.example.app")
    assert [(c.category, Path(c.path).name) for c in containers] == [
        (IOS_APP_GROUP_CATEGORY_NAME, GROUP_GUID),
        (IOS_BUNDLE_CATEGORY_NAME, BUNDLE_GUID),
        (IOS_DATA_CATEGORY_NAME, DATA_GUID),
        (IOS_EXTENSION_CATEGORY_NAME, WIDGET_GUID),
    ]


def test_resolve_labels_each_container_with_the_identifier_it_came_through(
    ios_acquisition: Path,
):
    """A shared group is named after the group, not after the app that reaches it."""
    by_category = {c.category: c for c in _resolver(ios_acquisition).resolve("com.example.app")}
    assert by_category[IOS_APP_GROUP_CATEGORY_NAME].identifier == GROUP_ID
    assert by_category[IOS_EXTENSION_CATEGORY_NAME].identifier == WIDGET_ID
    assert by_category[IOS_DATA_CATEGORY_NAME].identifier == APP_ID
    assert all(c.user_id is None for c in by_category.values())


def test_resolve_narrows_to_one_category(ios_acquisition: Path):
    containers = _resolver(ios_acquisition).resolve("com.example.app", IOS_DATA_CATEGORY_NAME)
    assert [Path(c.path).name for c in containers] == [DATA_GUID]


def test_resolve_accepts_the_team_prefixed_form_too(ios_acquisition: Path):
    resolver = _resolver(ios_acquisition)
    assert resolver.resolve(APP_ID) == resolver.resolve("com.example.app")


def test_resolve_is_deterministic(ios_acquisition: Path):
    resolver = _resolver(ios_acquisition)
    assert resolver.resolve("com.example.app") == resolver.resolve("com.example.app")


def test_unknown_package_resolves_to_nothing(ios_acquisition: Path):
    assert _resolver(ios_acquisition).resolve("com.nobody.here") == []


# --- No silent loss -------------------------------------------------------

def test_unknown_content_class_is_surfaced_not_dropped(ios_acquisition: Path):
    resolver = _resolver(ios_acquisition)
    containers = resolver.resolve("com.example.exotic")
    assert [c.category for c in containers] == [
        f"{UNKNOWN_CATEGORY_PREFIX}{UNKNOWN_CONTENT_CLASS}"
    ]
    assert Path(containers[0].path).name == UNKNOWN_CLASS_GUID
    kinds = [f.kind for f in resolver.findings]
    assert kinds.count(UNKNOWN_CONTENT_CLASS_FINDING_NAME) == 1


def test_container_without_metadata_is_counted_and_reported(ios_acquisition: Path):
    findings = [
        f for f in _resolver(ios_acquisition).findings
        if f.kind == MISSING_METADATA_FINDING_NAME
    ]
    assert len(findings) == 1
    assert findings[0].detail.endswith(ORPHAN_GUID)


def test_ambiguous_identifier_returns_all_containers_and_records_a_finding(
    ios_acquisition: Path,
):
    """Never first-wins: two data containers for one identifier both come back."""
    resolver = _resolver(ios_acquisition)
    containers = resolver.resolve("com.dup.app", IOS_DATA_CATEGORY_NAME)
    assert sorted(Path(c.path).name for c in containers) == sorted(DUPLICATE_GUIDS)
    findings = [f for f in resolver.findings if f.kind == AMBIGUOUS_IDENTIFIER_FINDING_NAME]
    assert len(findings) == 1
    assert "com.dup.app" in findings[0].detail
    assert all(guid in findings[0].detail for guid in DUPLICATE_GUIDS)


# --- The references database ---------------------------------------------

def test_entitlements_blob_yields_groups_and_keychain_groups(ios_acquisition: Path):
    """One binary-plist BLOB answers both the group question and the ``agrp`` question."""
    records = {r.identifier: r for r in _resolver(ios_acquisition).references.records}
    assert records[APP_ID].app_groups == (GROUP_ID,)
    assert records[APP_ID].keychain_groups == ("ABCDEFGHIJ.ABCDEFGHIJ.com.example.app",)


def test_child_bundles_link_the_widget_to_its_parent(ios_acquisition: Path):
    references = _resolver(ios_acquisition).references
    parent = next(r for r in references.records if r.identifier == APP_ID)
    assert references.children_by_parent[parent.row_id] == (WIDGET_ID,)
