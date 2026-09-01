"""Android resolution over a synthetic acquisition: six layouts, two users, one trap.

Defines:    tests that every storage layout is returned with its kind and user id, that a
            category narrows the result, and that neither a package-shaped file nor an app
            cache imitating another layout produces a phantom container.
Used by:    the test suite.
Depends on: the ``android_acquisition`` fixture in this directory's ``conftest`` and the
            public surface of ``model_atlas.ingest.utils.mobile_containers``.
"""
from __future__ import annotations

from pathlib import Path

from model_atlas.ingest.utils.mobile_containers import (
    ANDROID_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME,
    ANDROID_EXTERNAL_OBB_CATEGORY_NAME,
    ANDROID_USER_CATEGORY_NAME,
    ANDROID_USER_DE_CATEGORY_NAME,
    AndroidResolver,
    ZipOpener,
)
from tests.ingest.mobile_containers.conftest import ANDROID_PACKAGE, ANDROID_ROOT


def _resolver(archive: Path) -> AndroidResolver:
    with ZipOpener(archive) as opener:
        return AndroidResolver(opener)


def test_resolve_returns_every_layout_with_its_kind_and_user(android_acquisition: Path):
    containers = _resolver(android_acquisition).resolve(ANDROID_PACKAGE)
    assert [(c.category, c.user_id, c.path) for c in containers] == [
        (ANDROID_DATA_CATEGORY_NAME, 0, f"{ANDROID_ROOT}/data/data/{ANDROID_PACKAGE}"),
        (
            ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
            0,
            f"{ANDROID_ROOT}/data/media/0/Android/data/{ANDROID_PACKAGE}",
        ),
        (
            ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME,
            0,
            f"{ANDROID_ROOT}/data/media/0/Android/media/{ANDROID_PACKAGE}",
        ),
        (
            ANDROID_EXTERNAL_OBB_CATEGORY_NAME,
            0,
            f"{ANDROID_ROOT}/data/media/0/Android/obb/{ANDROID_PACKAGE}",
        ),
        (ANDROID_USER_CATEGORY_NAME, 10, f"{ANDROID_ROOT}/data/user/10/{ANDROID_PACKAGE}"),
        (ANDROID_USER_DE_CATEGORY_NAME, 0, f"{ANDROID_ROOT}/data/user_de/0/{ANDROID_PACKAGE}"),
        (ANDROID_USER_DE_CATEGORY_NAME, 10, f"{ANDROID_ROOT}/data/user_de/10/{ANDROID_PACKAGE}"),
    ]


def test_data_data_is_attributed_to_user_zero(android_acquisition: Path):
    """``/data/data`` is a bind of ``/data/user/0``, so its user id is 0, not unknown."""
    containers = _resolver(android_acquisition).resolve(
        ANDROID_PACKAGE, ANDROID_DATA_CATEGORY_NAME
    )
    assert [c.user_id for c in containers] == [0]


def test_per_user_duplicates_of_one_category_are_ordered_by_user(android_acquisition: Path):
    containers = _resolver(android_acquisition).resolve(
        ANDROID_PACKAGE, ANDROID_USER_DE_CATEGORY_NAME
    )
    assert [c.user_id for c in containers] == [0, 10]


def test_a_cache_path_imitating_another_layout_is_not_a_container(
    android_acquisition: Path,
):
    """``…/<pkg>/cache/data/user/0/<pkg>`` is app data, not a second user-0 container."""
    containers = _resolver(android_acquisition).resolve(ANDROID_PACKAGE)
    assert not any("/cache/" in c.path for c in containers)
    assert not any(c.category == ANDROID_USER_CATEGORY_NAME and c.user_id == 0
                   for c in containers)


def test_a_file_named_like_a_package_is_not_a_container(android_acquisition: Path):
    """``Android/data/.nomedia`` is a file: nothing exists inside it, so it is no container."""
    assert _resolver(android_acquisition).resolve(".nomedia") == []


def test_unrelated_trees_are_ignored(android_acquisition: Path):
    """``/data/app`` and ``/data/misc`` mention the package but are not its storage."""
    paths = [c.path for c in _resolver(android_acquisition).resolve(ANDROID_PACKAGE)]
    assert not any("/data/app/" in p or "/data/misc/" in p for p in paths)


def test_unknown_package_resolves_to_nothing(android_acquisition: Path):
    assert _resolver(android_acquisition).resolve("com.nobody.here") == []


def test_resolve_is_deterministic(android_acquisition: Path):
    resolver = _resolver(android_acquisition)
    assert resolver.resolve(ANDROID_PACKAGE) == resolver.resolve(ANDROID_PACKAGE)
