"""Every observation as ONE deterministic British-English sentence.

Defines:    ``verbalize(row) -> str`` — renders one flat-view row (the flat-view
            column contract) as a single sentence assembled from the ``phrase``
            fragments declared in ``model_atlas.core.vocabularies``. The owner's
            target shape, one clause per corner plus the attribution: *"The device
            <identifier> (referring <concerns>; the caller) <entity_link phrase>
            <bearer>, <spatial_link phrase> <place>, <occupancy phrase> <timespan>,
            <attestation phrase>."* — e.g. *"The device +41791234567 (the caller) was
            served by 228-01-2042-77341, near 46.205, 6.138, continuously during the
            interval from A to B, as recorded by a system."*
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
- **Negation reads as negation**: the fragments encode it (``absent`` -> "not at"),
  and the assembler never softens a phrase. ``references`` -> "referring to" (never
  presence); ``unknown`` members carry their own honest fragments rather than being
  omitted, so an undetermined relation is SAID rather than silently reading as presence.
- **Attribution never becomes weight**: the trailing ``attestation`` clause names the KIND
  of party that asserts ("as stated by a person"), never how much the claim is worth —
  that is a court question the model does not answer.
- **No invented facts**: an empty spatial corner is SAID ("with no recorded
  position"), an empty temporal corner likewise ("with no recorded time"); the
  ``(referring <concerns>)`` clause is omitted when concerns is NULL; a purely
  typed mention (NULL identifier) falls back to the entity type ("The device").
- **Deterministic**: a pure function of the row's values — the same row always
  yields the same sentence, byte for byte.

Sentence shape (why the spatial fragment carries no verb): ``spatial_link``'s fragments are
BARE relational phrases ("at", "near", "not at"). When a bearer is named the entity-link
clause supplies the verb ("was served by <bearer>, near <place>"); when the entity holds its
own position there is no bearer clause and the sentence supplies it ("was at <place>"). A
fragment carrying its own verb would produce "was served by X, was near Y".
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
    role = _cell(row, "activity_role")
    if role is not None:
        qualifiers.append(V.ActivityRole(role).phrase)
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
    entity_phrase = _link_phrase(row, "entity_link", V.EntityLink)
    spatial_phrase = _link_phrase(row, "spatial_link", V.SpatialLink)
    occupancy_phrase = _link_phrase(row, "occupancy", V.Occupancy)
    attestation_phrase = _link_phrase(row, "attestation", V.Attestation)

    # The bearer clause: "was served by <bearer>". Rendered only when a bearer is actually
    # named AND the verb is not `self` — for `self` the entity holds its own position, so
    # there is no second party and the spatial fragment carries the verb instead.
    bearer = _cell(row, "bearer_entity") or _cell(row, "bearer_raw_identifier")
    bearer_clause: str | None = None
    if (bearer is not None and entity_phrase is not None
            and _cell(row, "entity_link") != V.EntityLink.SELF.value):
        bearer_clause = f"{entity_phrase} {bearer}"

    # The trailing attribution. Self-contained by construction ("as recorded by a system"),
    # because no column carries the attestor's identity — the model records the KIND of party
    # that asserts, never who they are and never what the claim is worth.
    tail_attribution = f", {attestation_phrase}" if attestation_phrase is not None else ""

    # Both corners empty: the sentence still exists and SAYS both absences.
    if place is None and timespan is None:
        head = f"{subject} {bearer_clause}" if bearer_clause else subject
        return f"{head}, {NO_POSITION_WORDING} and {NO_TIME_WORDING}{tail_attribution}."

    # No place: the spatial fragment describes a position that is not there, so only the
    # said absence and the timespan render (never an invented position).
    if place is None:
        body, is_instant = timespan
        head = f"{subject} {bearer_clause}" if bearer_clause else subject
        connector = occupancy_phrase or (V.TemporalKind.INSTANT.phrase if is_instant
                                         else "during")
        return (f"{head}, {NO_POSITION_WORDING}, {connector} "
                f"{body}{tail_attribution}.")

    # The spatial fragment is BARE ("at", "near", "not at"), so the verb comes from the
    # bearer clause when there is one and from the sentence otherwise.
    if spatial_phrase is None:
        spatial = f"{_UNLINKED_POSITION_WORDING} {place}"
    elif bearer_clause is not None:
        spatial = f"{bearer_clause}, {spatial_phrase} {place}"
    else:
        spatial = f"was {spatial_phrase} {place}"

    # No timespan: the occupancy fragment quantifies an interval that is not there, so it
    # drops; the absence is said instead.
    if timespan is None:
        return f"{subject} {spatial}, {NO_TIME_WORDING}{tail_attribution}."

    body, is_instant = timespan
    # Occupancy fragments end in their own preposition ("continuously during"), so the
    # timespan body follows directly. Without one, an instant reads "at T" and an interval
    # "during the interval from A to B".
    connector = occupancy_phrase or (V.TemporalKind.INSTANT.phrase if is_instant else "during")
    return f"{subject} {spatial}, {connector} {body}{tail_attribution}."
