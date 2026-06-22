"""Anchored, prefix-tolerant path/name matching for input selectors.

Defines:    path_matches (segment-wise, full-path, with {uuid}/* wildcards and a
            leading-segment skip budget) and name_matches (exact basename equality).
Used by:    presets.matcher (the path/name pre-filter) and presets.spec (validating a
            selector ``path`` at preset-load time).
Depends on: standard library only.

A folder and an archive are both addressed by a logical path inside a Container, so a
selector path matches the same way in either. Acquisition tools wrap the filesystem in
one root folder (``filesystem1/``, ``_/``, ``Dump/``); ``root_prefix_depth`` skips up to
that many leading candidate segments so ``/private/...`` matches
``filesystem1/private/...`` without the preset author knowing the wrapper name.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# One segment equal to a canonical UUID (the iOS device id directory under FFS).
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def _selector_segments(selector_path: str) -> list[str]:
    """Normalise a selector path to POSIX segments, rejecting the ``**`` glob.

    ``path`` is a full anchored path, not a roaming glob, so ``**`` is a hard error
    (raised here and re-used by the preset parser to fail at load time)."""
    normalised = selector_path.replace("\\", "/").lstrip("/")
    segments = [seg for seg in normalised.split("/") if seg != ""]
    for seg in segments:
        if "**" in seg:
            raise ValueError(
                f"selector path {selector_path!r} uses '**', which is not allowed; "
                f"a path is a full anchored path (use '*' for one segment or '{{uuid}}')"
            )
        if "{" in seg or "}" in seg:
            if seg != "{uuid}":
                raise ValueError(
                    f"selector path {selector_path!r} segment {seg!r} is not a valid "
                    f"placeholder; only '{{uuid}}' is supported"
                )
    return segments


def validate_selector_path(selector_path: str) -> None:
    """Raise ValueError if ``selector_path`` violates the segment grammar (used at preset
    load time so a bad path fails fast)."""
    _selector_segments(selector_path)


def _segment_matches(selector_seg: str, candidate_seg: str) -> bool:
    if selector_seg == "*":
        return True  # exactly one whole segment, any text (no '/')
    if selector_seg == "{uuid}":
        return bool(_UUID_RE.match(candidate_seg))
    return selector_seg == candidate_seg


def path_matches(
    selector_path: str, candidate: PurePosixPath, *, root_prefix_depth: int = 1
) -> bool:
    """Segment-wise, anchored match of ``selector_path`` against ``candidate``.

    For ``skip`` in ``0..root_prefix_depth`` (inclusive) the first ``skip`` candidate
    segments are dropped and the remainder is matched segment-for-segment against the
    selector (equal counts required — this is a full-path match, not a suffix search).
    The first ``skip`` that matches wins. ``*`` matches one segment; ``{uuid}`` matches a
    UUID segment; every other selector segment matches its literal text.
    """
    selector_segments = _selector_segments(selector_path)
    candidate_segments = list(candidate.parts)

    for skip in range(0, root_prefix_depth + 1):
        remainder = candidate_segments[skip:]
        if len(remainder) != len(selector_segments):
            continue
        if all(_segment_matches(sel, cand) for sel, cand in zip(selector_segments, remainder)):
            return True
    return False


def name_matches(selector_name: str, candidate: PurePosixPath) -> bool:
    """Exact basename equality — no globbing (the easy CSV/AXIOM-export case)."""
    return candidate.name == selector_name
