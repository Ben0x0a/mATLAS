"""Where does this app keep its data? — a standalone iOS/Android container resolver.

Defines:    the library's whole public surface — ``build_resolver`` and
            ``detect_platform`` (the platform-agnostic entry point), the ``Resolver``
            protocol both platforms satisfy, ``Container``, ``Finding``, the ``Opener``
            protocol with its ``ZipOpener`` / ``DirectoryOpener`` implementations, the
            two concrete resolvers, and the category names both platforms label with.
Used by:    ``model_atlas.ingest`` (a preset naming an app rather than a path) and any
            caller that wants the same answer on its own.
Depends on: the standard library ONLY — ``plistlib``, ``sqlite3``, ``pathlib``,
            ``zipfile``. Nothing in this package imports ``model_atlas`` (a test asserts
            it), which is what keeps it liftable into its own distribution by a ``git mv``
            plus a ``pyproject`` entry rather than a rewrite.

The one place this package departs from the house style is its use of **explicit relative
imports** between its own modules, where the rest of mATLAS uses absolute ones. That is the
detachability rule in practice: an absolute ``from model_atlas.ingest.utils.mobile_containers.…``
would hard-code the package's current address into every module, so moving it would mean
rewriting each import — exactly the rewrite this arrangement exists to avoid. PEP 8 allows
explicit relative imports for precisely this case.

The one call a caller needs, whichever platform produced the acquisition::

    with ZipOpener(acquisition) as opener:
        resolver = build_resolver(opener)          # detects iOS or Android
        locations = resolver.resolve("com.sbb.ch")  # every container it owns
        oddities = resolver.findings                # never a silent drop

The shared contract, identical on both platforms::

    resolve(package: str, category: str | None = None) -> list[Container]
    findings -> tuple[Finding, ...]

Without a category, every location the package owns comes back, each labelled with its
own category. With one, the result is narrowed to that kind. Both resolvers are built once
per acquisition from an injected ``Opener`` — the acquisition is opened a single time,
streamed, and never modified or extracted wholesale.

Detection lives in this library rather than in the caller because "which platform is this?"
is answerable only from the same evidence the resolvers already read; a copy of the rules
outside would drift, and a detached copy of the library would be unable to answer its own
question.

Why the library exists: on iOS an app's data lives under per-install GUIDs, so no preset
can hard-code a path, and one app's data is spread across several containers at once — the
SBB app occupies four (bundle, data, shared group, widget) and Snapchat twelve. On Android
the paths are stable but multiply per user. Both questions have one answer shape, so they
share one contract.
"""
from __future__ import annotations

from .android.resolver import AndroidResolver
from .categories import (
    ANDROID_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_DATA_CATEGORY_NAME,
    ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME,
    ANDROID_EXTERNAL_OBB_CATEGORY_NAME,
    ANDROID_USER_CATEGORY_NAME,
    ANDROID_USER_DE_CATEGORY_NAME,
    IOS_APP_GROUP_CATEGORY_NAME,
    IOS_BUNDLE_CATEGORY_NAME,
    IOS_DATA_CATEGORY_NAME,
    IOS_EXTENSION_CATEGORY_NAME,
)
from .ios.resolver import IosResolver
from .platforms import (
    ANDROID_PLATFORM_NAME,
    IOS_PLATFORM_NAME,
    Resolver,
    UnsupportedAcquisitionError,
    build_resolver,
    detect_platform,
)
from .models import Container, Finding
from .openers import (
    DirectoryOpener,
    Opener,
    ZipOpener,
)

__all__ = [
    "build_resolver",
    "detect_platform",
    "Resolver",
    "UnsupportedAcquisitionError",
    "ANDROID_PLATFORM_NAME",
    "IOS_PLATFORM_NAME",
    "AndroidResolver",
    "Container",
    "DirectoryOpener",
    "Finding",
    "IosResolver",
    "Opener",
    "ZipOpener",
    "ANDROID_DATA_CATEGORY_NAME",
    "ANDROID_EXTERNAL_DATA_CATEGORY_NAME",
    "ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME",
    "ANDROID_EXTERNAL_OBB_CATEGORY_NAME",
    "ANDROID_USER_CATEGORY_NAME",
    "ANDROID_USER_DE_CATEGORY_NAME",
    "IOS_APP_GROUP_CATEGORY_NAME",
    "IOS_BUNDLE_CATEGORY_NAME",
    "IOS_DATA_CATEGORY_NAME",
    "IOS_EXTENSION_CATEGORY_NAME",
]
