"""Every observation as ONE deterministic British-English sentence.

Defines:    ``verbalize(row) -> str`` — renders one flat-view row (the flat-view
            column contract) as a single sentence assembled from the ``phrase``
            fragments declared in ``model_atlas.core.vocabularies``. The owner's
            target shape (run_db_schema.md): *"Entity <identifier>
            (referring <concerns>) was <entity_position_link phrase> at <place>
            <spatial_temporal_link phrase> and <entity_time_link phrase> from
            <lower> to <upper>"* — or *"at <instant>"* when the timespan is an
            instant.
Used by:    consumers wired later (the bonus CSV column, the GUI
            panels — one function, called everywhere); tests.core.test_verbalize.
Depends on: ``model_atlas.core.vocabularies`` (the ``phrase`` fragments — declared
            there once, never redeclared here) and ``pandas`` (NA detection for
            cells coming from the projected frame).

Rendering rules (all requirements from the target sentence shape above, restated
where the code enforces them):

- **Place** = the integration values when present (``latitude_wgs84`` /
  ``longitude_wgs84`` — which also covers a method's named substitution, e.g. a
  cell register or a place centroid resolving a raw reference to coordinates),
  combined with the expression's verbatim raw (``raw_position``) when both exist;
  the verbatim raw alone is the fallback (address, place name, cell ref).
- **Instant vs interval** comes from the timespan itself, not a stored kind (the
  the flat view carries no ``temporal_kind`` column): equal or lone bounds render
  "at <instant>", two distinct bounds render "the interval from <lower> to
  <upper>". Each bound prefers the integrated offset-tagged ISO string (its
  ``N/A`` assumed-UTC sentinel stays visible), then the verbatim
  raw, then the canonical epoch microseconds.
- **Negation reads as negation**: the fragments already encode it
  (``never_during_interval`` -> "was never present during"); the assembler never
  softens a phrase. ``references`` -> "referred to" (never presence);
  ``claimed_at`` -> "was claimed to be at" (a claim); ``unknown`` members carry
  their own honest fragments.
- **No invented facts**: an empty spatial corner is SAID ("with no recorded
  position"), an empty temporal corner likewise ("with no recorded time"); the
  ``(referring <concerns>)`` clause is omitted when concerns is NULL; a purely
  typed mention (NULL identifier) falls back to the entity type ("The device").
- **Deterministic**: a pure function of the row's values — the same row always
  yields the same sentence, byte for byte.

Sentence shape (why the commas around ``and <entity_time_link phrase>``): the
owner's template chains the S-T fragment ("continuously during") and the E-T
fragment ("acted at") in front of ONE shared timespan. Setting the two fragments
off with commas makes that shared-object reading explicit — *"was within range of
<place> continuously during, and acted at, the interval from <lower> to
<upper>"* — so every fragment lands verbatim and the sentence stays parseable.
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from model_atlas.core import vocabularies as V

# The said-absence wordings (an empty corner is stated, never filled).
# Consumed by verbalize() below and asserted verbatim by tests.core.test_verbalize.
NO_POSITION_WORDING = "with no recorded position"
NO_TIME_WORDING = "with no recorded time"

# Neutral glue for a row whose place exists but whose E-S link is NULL: states that
# a position was recorded without asserting HOW the entity relates to it (using the
# UNKNOWN member's phrase instead would invent a stored link value).
_UNLINKED_POSITION_WORDING = "had a recorded position of"

# Each temporal bound renders from the first populated column in this preference
# order: the integrated ISO string (human-readable, offset-tagged, sentinel-loud),
# the verbatim raw, then the canonical epoch microseconds.
_BOUND_COLUMN_SUFFIXES = ("datetime_iso", "raw", "unix_utc_us")


def _cell(row: Mapping[str, Any] | pd.Series, column: str) -> Any | None:
    """One flat-view cell, normalised so every flavour of 'absent' is ``None``.

    HOW: ``.get`` covers both accepted row types (a plain mapping and a pandas
    Series, whose missing-label ``.get`` also returns None), then ``pd.isna``
    folds the frame's NA flavours (``nan``, ``pd.NA``, ``NaT``) into None.
    WHY: the caller may hand a sparse dict (absent column) or a projected frame
    row (NA cell); both must read as "no recorded value", never crash.
    """
    value = row.get(column)
    if value is None or pd.isna(value):
        return None
    return value


def _coordinate(value: Any) -> str:
    """A WGS84 coordinate as its shortest round-trip decimal text.

    WHY repr(float): the shortest string that parses back to the exact double —
    deterministic, and identical whether the cell arrived as a Python float or a
    numpy scalar from the projected frame.
    """
    return repr(float(value))


def _subject(row: Mapping[str, Any] | pd.Series) -> str:
    """The sentence subject: identifier, else the entity type, plus qualifiers.

    A purely typed mention (NULL identifier) falls back to the type
    ("The device"); ``concerns`` and the role phrase join in one parenthesis, each
    omitted when absent — never invented.
    """
    identifier = _cell(row, "entity")
    entity_type = _cell(row, "entity_type")
    if identifier is not None:
        head = f"Entity {identifier}"
    elif entity_type is not None and entity_type != V.EntityType.UNKNOWN.value:
        head = f"The {entity_type}"
    else:
        # Both corners of the subject are unknown: say so rather than render
        # "The unknown" (the type token is a vocabulary value, not prose).
        head = "An unknown entity"
    qualifiers: list[str] = []
    concerns = _cell(row, "concerns")
    if concerns is not None:
        qualifiers.append(f"referring {concerns}")
    role = _cell(row, "entity_role")
    if role is not None:
        qualifiers.append(V.EntityRole(role).phrase)
    if qualifiers:
        head += " (" + "; ".join(qualifiers) + ")"
    return head


def _place(row: Mapping[str, Any] | pd.Series) -> str | None:
    """The place text, or None when the spatial corner carries no value.

    Integration coordinates win when present (they also carry a method's named
    substitution — a geocoded address, a cell register's tower position); the
    verbatim raw joins them for readability and is the sole fallback otherwise.
    """
    latitude = _cell(row, "latitude_wgs84")
    longitude = _cell(row, "longitude_wgs84")
    raw = _cell(row, "raw_position")
    if latitude is not None and longitude is not None:
        coordinates = f"{_coordinate(latitude)}, {_coordinate(longitude)}"
        if raw is not None:
            return f"{raw} ({coordinates})"
        return coordinates
    if raw is not None:
        return str(raw)
    return None


def _bound_text(row: Mapping[str, Any] | pd.Series, which: str) -> str | None:
    """One temporal bound's display text (``which`` is ``lower`` or ``upper``)."""
    for suffix in _BOUND_COLUMN_SUFFIXES:
        value = _cell(row, f"time_{which}_{suffix}")
        if value is not None:
            return str(value)
    return None


def _timespan(row: Mapping[str, Any] | pd.Series) -> tuple[str, bool] | None:
    """The timespan body and its shape: ``(text, is_instant)``, or None when empty.

    Equal or lone bounds are an instant (the brief's rule — the flat view carries
    no stored ``temporal_kind``, so the shape derives from the bounds themselves);
    two distinct bounds are an interval, introduced by the INTERVAL fragment.
    Equality compares the RENDERED texts: two bounds that display identically ARE
    one instant to the reader, whatever their storage differences.
    """
    lower = _bound_text(row, "lower")
    upper = _bound_text(row, "upper")
    if lower is None and upper is None:
        return None
    if lower is None or upper is None or lower == upper:
        instant = lower if lower is not None else upper
        assert instant is not None  # Invariant: at least one bound exists here.
        return instant, True
    return f"the interval {V.TemporalKind.INTERVAL.phrase} {lower} to {upper}", False


def _link_phrase(row: Mapping[str, Any] | pd.Series, column: str,
                 vocabulary: type) -> str | None:
    """The ``phrase`` fragment for a link column, or None when the link is NULL.

    An invalid stored token raises from the enum lookup on purpose: the DB CHECK
    guarantees closed-vocabulary values, so a failure here is a real derivation
    bug that must surface, not be prettified away.
    """
    value = _cell(row, column)
    if value is None:
        return None
    return vocabulary(value).phrase


def verbalize(row: Mapping[str, Any] | pd.Series) -> str:
    """Render one observation (a flat-view row) as one deterministic sentence.

    ``row`` is a flat-view row: a pandas Series (one row of
    ``flat_view.build_flat_view``'s frame) or any dict-like mapping of column name to
    value — absent columns and NA cells both read as "no recorded value".
    """
    subject = _subject(row)
    place = _place(row)
    timespan = _timespan(row)
    position_phrase = _link_phrase(row, "entity_position_link", V.EntityPositionLink)
    time_phrase = _link_phrase(row, "entity_time_link", V.EntityTimeLink)
    spatial_temporal_phrase = _link_phrase(
        row, "spatial_temporal_link", V.SpatialTemporalLink)

    # Both corners empty: the sentence still exists and SAYS both absences.
    if place is None and timespan is None:
        return f"{subject}, {NO_POSITION_WORDING} and {NO_TIME_WORDING}."

    # No place: the E-S and S-T fragments describe a position that is not there,
    # so only the said absence and the temporal predicate render (never invented).
    if place is None:
        body, is_instant = timespan
        if time_phrase is not None:
            # The E-T fragments end in their own preposition ("was observed at",
            # "was never present during"), so the timespan body follows directly.
            tail = f"{time_phrase} {body}"
        elif is_instant:
            tail = f"{V.TemporalKind.INSTANT.phrase} {body}"
        else:
            tail = f"during {body}"
        return f"{subject}, {NO_POSITION_WORDING}, {tail}."

    spatial = (f"{position_phrase} {place}" if position_phrase is not None
               else f"{_UNLINKED_POSITION_WORDING} {place}")

    # No timespan: the S-T fragment relates the place to an interval that is not
    # there, and the E-T fragments dangle without a time object — both drop; the
    # absence is said instead.
    if timespan is None:
        return f"{subject} {spatial}, {NO_TIME_WORDING}."

    body, is_instant = timespan
    if time_phrase is not None:
        # The owner's template chains "<st phrase> and <et phrase>" before ONE
        # shared timespan; the commas make that shared-object reading explicit
        # ("continuously during, and acted at, the interval from A to B").
        st_part = f" {spatial_temporal_phrase}" if spatial_temporal_phrase else ""
        return f"{subject} {spatial}{st_part}, and {time_phrase}, {body}."
    if spatial_temporal_phrase is not None:
        # No E-T link: the S-T fragment alone introduces the timespan ("at T",
        # "continuously during the interval from A to B").
        return f"{subject} {spatial} {spatial_temporal_phrase} {body}."
    connector = V.TemporalKind.INSTANT.phrase if is_instant else "during"
    return f"{subject} {spatial} {connector} {body}."
