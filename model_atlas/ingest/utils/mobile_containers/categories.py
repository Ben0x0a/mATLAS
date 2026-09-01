"""Every category name this library can emit, and the iOS content-class table.

Defines:    the ``*_CATEGORY_NAME`` constants for both platforms, the iOS
            ``CONTENT_CLASS_CATEGORIES`` map (``MCMMetadataContentClass`` -> category),
            ``category_for_content_class`` and the ``*_FINDING_NAME`` tags.
Used by:    ``mobile_containers.ios.metadata`` / ``.resolver`` and
            ``mobile_containers.android.resolver``; callers compare the ``category`` of a
            returned ``Container`` against these constants.
Depends on: the standard library only.

One module holds every classification decision so a reader can see the whole vocabulary
at once and a new observed value has exactly one place to be added — the recognition
tables must not be duplicated per call site.

The iOS map is deliberately **data, not a closed enum**. Apple's content classes are an
internal numbering that gains members across OS releases, so an integer that is not in
the table is given a distinguishable ``class-<n>`` category AND recorded as a finding —
never dropped, and never silently folded into a neighbouring category. The seven entries
below were each observed on a real iOS 17 full-filesystem acquisition together with the
container tree they label, which is how the names were chosen.
"""
from __future__ import annotations

# --- iOS categories -------------------------------------------------------
# The path each class labels on the device is given so the name can be re-derived.
IOS_BUNDLE_CATEGORY_NAME = "bundle"                  # /private/var/containers/Bundle/Application
IOS_DATA_CATEGORY_NAME = "data"                      # /private/var/mobile/Containers/Data/Application
IOS_EXTENSION_CATEGORY_NAME = "extension"            # /private/var/mobile/Containers/Data/PluginKitPlugin
IOS_APP_GROUP_CATEGORY_NAME = "app_group"            # /private/var/mobile/Containers/Shared/AppGroup
IOS_INTERNAL_DAEMON_CATEGORY_NAME = "internal_daemon"  # …/Containers/Data/InternalDaemon
IOS_SYSTEM_DATA_CATEGORY_NAME = "system_data"        # /private/var/containers/Data/System
IOS_SYSTEM_GROUP_CATEGORY_NAME = "system_group"      # /private/var/containers/Shared/SystemGroup

CONTENT_CLASS_CATEGORIES: dict[int, str] = {
    1: IOS_BUNDLE_CATEGORY_NAME,
    2: IOS_DATA_CATEGORY_NAME,
    4: IOS_EXTENSION_CATEGORY_NAME,
    7: IOS_APP_GROUP_CATEGORY_NAME,
    10: IOS_INTERNAL_DAEMON_CATEGORY_NAME,
    12: IOS_SYSTEM_DATA_CATEGORY_NAME,
    13: IOS_SYSTEM_GROUP_CATEGORY_NAME,
}

# Category given to a content class absent from the table above. The number is kept in
# the name so two unknown classes never collapse into one bucket.
UNKNOWN_CATEGORY_PREFIX = "class-"

# --- Android categories ---------------------------------------------------
# The layout each name labels is given so the mapping can be checked against a device.
ANDROID_DATA_CATEGORY_NAME = "data"                      # /data/data/<pkg>
ANDROID_USER_CATEGORY_NAME = "user"                      # /data/user/<uid>/<pkg>
ANDROID_USER_DE_CATEGORY_NAME = "user_de"                # /data/user_de/<uid>/<pkg>
ANDROID_EXTERNAL_DATA_CATEGORY_NAME = "external_data"    # /data/media/<uid>/Android/data/<pkg>
ANDROID_EXTERNAL_MEDIA_CATEGORY_NAME = "external_media"  # /data/media/<uid>/Android/media/<pkg>
ANDROID_EXTERNAL_OBB_CATEGORY_NAME = "external_obb"      # /data/media/<uid>/Android/obb/<pkg>

# --- Finding tags ---------------------------------------------------------
UNKNOWN_CONTENT_CLASS_FINDING_NAME = "unknown-content-class"
MISSING_METADATA_FINDING_NAME = "container-without-metadata"
UNREADABLE_METADATA_FINDING_NAME = "unreadable-metadata"
AMBIGUOUS_IDENTIFIER_FINDING_NAME = "ambiguous-identifier"
UNREADABLE_ENTITLEMENTS_FINDING_NAME = "unreadable-entitlements"
MISSING_REFERENCES_FINDING_NAME = "missing-references-database"
# Android. The two platforms share AMBIGUOUS_IDENTIFIER_FINDING_NAME above: "one identifier
# resolved to two containers of the same kind" means the same thing on both, and a caller
# auditing findings should not have to learn two tags for one condition.
MISSING_DATA_TREE_FINDING_NAME = "missing-data-tree"
UNPARSABLE_USER_ID_FINDING_NAME = "unparsable-user-id"


def category_for_content_class(content_class: int) -> tuple[str, bool]:
    """Map an ``MCMMetadataContentClass`` to a category name.

    Returns ``(category, known)``. ``known`` is False for a class absent from
    ``CONTENT_CLASS_CATEGORIES``, which the caller must turn into a recorded finding —
    an unrecognised class means the device carries a container tree this library has
    never seen, and dropping it would hide evidence rather than merely mislabel it.
    """
    known = CONTENT_CLASS_CATEGORIES.get(content_class)
    if known is not None:
        return known, True
    return f"{UNKNOWN_CATEGORY_PREFIX}{content_class}", False
