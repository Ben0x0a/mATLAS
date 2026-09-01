"""The one rule by which an iOS bundle id is matched against a stored identifier.

Defines:    ``strip_team_prefix`` (drop a leading ``<TeamID>.`` when one is present) and
            ``identifiers_match`` (the comparison every lookup in this package uses).
Used by:    ``mobile_containers.ios.metadata`` (container lookups by identifier) and
            ``mobile_containers.ios.resolver`` (matching a package against
            ``code_signing_info``); both must agree, hence one shared module.
Depends on: the standard library only.

WHY this exists at all: the same app is written two different ways in the two sources this
library joins. A container's ``MCMMetadataIdentifier`` may carry the developer's team id —
``5Q4J53EFRC.com.sbb.ch`` — while a preset, a user, or another table names the app plainly
as ``com.sbb.ch``. Both must resolve to the same containers.

WHY a shape-checked, boundary-anchored strip rather than a substring or suffix match: a
substring match would associate ``com.sbb.ch`` with any identifier merely containing it,
and an unconstrained "drop the first segment" would let ``sbb.ch`` match ``com.sbb.ch`` —
both mis-attribute one app's data to another, which in a forensic result is worse than
finding nothing. An Apple team identifier is exactly ten upper-case alphanumerics, so
requiring that shape keeps the strip to the case it was written for: on the verified
acquisition only 2 of 1088 recorded identifiers carry such a prefix, and stripping them
produced no collisions.
"""
from __future__ import annotations

import re

# A leading Apple team identifier: exactly ten upper-case alphanumerics, then a dot.
_TEAM_PREFIX_RE = re.compile(r"^[A-Z0-9]{10}\.")


def strip_team_prefix(identifier: str) -> str:
    """``5Q4J53EFRC.com.sbb.ch`` -> ``com.sbb.ch``; anything else unchanged."""
    match = _TEAM_PREFIX_RE.match(identifier)
    if match is None:
        return identifier
    return identifier[match.end():]


def identifiers_match(left: str, right: str) -> bool:
    """True when two identifiers name the same app, ignoring a team-id prefix on either.

    Symmetric on purpose: the caller may hold either form, and so may the acquisition.
    """
    return strip_team_prefix(left) == strip_team_prefix(right)
