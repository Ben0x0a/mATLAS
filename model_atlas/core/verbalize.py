"""Every observation as ONE deterministic British-English sentence.

Defines:    ``verbalize(row) -> str`` — renders one flat-view row (the flat-view
            column contract) as a single sentence assembled from the ``phrase``
            fragments declared in ``model_atlas.core.vocabularies``. The shape, one
            clause per arm (decision 55): *"Entity <identifier> (referring <concerns>)
            <spatial_link phrase> <bearer>, at <place>, <temporal_link phrase>
            <timespan>."* — e.g. *"Entity +41791234567 was served by 228-01-2042-77341,
            at 46.205, 6.138, at least once during the interval from A to B."* When the
            entity holds its own position the spatial verb takes the place directly:
            *"Entity X was measured at 46.205, 6.138, at T."*
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
  with ``position_raw`` reproduced VERBATIM as the FALLBACK when there are none;
  the verbatim raw alone is the fallback (address, place name, cell ref).
- **Instant vs interval** comes from the timespan itself, not a stored kind (the
  the flat view carries no ``temporal_kind`` column): equal or lone bounds render
  "at <instant>", two distinct bounds render "the interval from <lower> to
  <upper>". Each bound prefers the integrated offset-tagged ISO string (its
  ``N/A`` assumed-UTC sentinel stays visible), then the verbatim
  raw, then the canonical epoch microseconds.
- **Negation reads as negation**: only the temporal link carries it, and its fragments
  say so ("never during", "not at"); the assembler never softens a phrase. ``unknown``
  members carry their own honest fragments rather than being omitted, so an undetermined
  relation is SAID rather than silently reading as presence.
- **No attribution clause**: the source link is the record chain, not a vocabulary, so the
  sentence states no "as recorded by" — and never how much the claim is worth.
- **No invented facts**: an empty spatial corner is SAID ("with no recorded
  position"), an empty temporal corner likewise ("with no recorded time"); the
  ``(referring <concerns>)`` clause is omitted when concerns is NULL; a purely
  typed mention (NULL identifier) falls back to the entity type ("The device").
- **Deterministic**: a pure function of the row's values — the same row always
  yields the same sentence, byte for byte.

Sentence shape: ``spatial_link``'s fragments are VERB phrases taking one object. When
another entity holds the position (``party``, or a ``mentioned`` row naming one) the object
is that bearer and the place follows ("was served by <antenna>, at <place>"); otherwise the
object is the place itself ("was measured at <place>"). ``entity_link`` adds a qualifier
only where the structure cannot show it: ``mentioned`` and ``unknown``.
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from model_atlas.core import vocabularies as V
from model_atlas.core.local_time import render_local

# The said-absence wordings (an empty corner is stated, never filled).
# Consumed by verbalize() below and asserted verbatim by tests.core.test_verbalize.
NO_POSITION_WORDING = "with no recorded position"
NO_TIME_WORDING = "with no recorded time"

# Neutral glue for a row whose place exists but whose spatial link is NULL: states that
# a position was recorded without asserting HOW the observation uses it (using the
# UNKNOWN member's phrase instead would invent a stored link value).
_UNLINKED_POSITION_WORDING = "had a recorded position of"

# A bound with a canonical instant renders through ``core.local_time`` (the ONE renderer,
# shared with the concomitance report) from the epoch µs plus the row's offset to local.
# There is no stored ISO column to read any more — see local_time's module docstring.
# Only a bound with NO instant falls back, to the verbatim raw: that is L1 evidence the
# integration could not read, and showing it beats saying nothing.
_BOUND_FALLBACK_SUFFIXES = ("raw",)


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
    omitted when absent — never invented. The role phrase renders only for the roles the
    sentence structure cannot show (``mentioned``, ``unknown``).
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
    entity_link = _cell(row, "entity_link_matlas")
    if entity_link in (V.EntityLink.MENTIONED.value, V.EntityLink.UNKNOWN.value):
        qualifiers.append(V.EntityLink(entity_link).phrase)
    if qualifiers:
        head += " (" + "; ".join(qualifiers) + ")"
    return head


def _place(row: Mapping[str, Any] | pd.Series) -> str | None:
    """The place text, or None when the spatial corner carries no value.

    Integration coordinates win when present (they also carry a method's named
    substitution — a geocoded address, a cell register's tower position), and
    ``position_raw`` is the FALLBACK, not a companion, and is reproduced verbatim.

    WHY it stopped being a companion: ``position_raw`` records the components that PRODUCED
    the coordinates, so joining the two would state one coordinate twice — "was at
    46.19, 6.12 (ZLATITUDE:46.193000 | ZLONGITUDE:6.129000)". The two are therefore
    alternatives, and because a row only has ``position_raw`` when it also has coordinates,
    this fallback is reached only by a caller supplying the column without them.
    """
    latitude = _cell(row, "latitude_wgs84")
    longitude = _cell(row, "longitude_wgs84")
    if latitude is not None and longitude is not None:
        return f"{_coordinate(latitude)}, {_coordinate(longitude)}"
    # The fallback renders ``position_raw`` VERBATIM, labels and separators included.
    # WHY no reformatting: splitting on `` | `` and stripping the ``column:`` labels would
    # make the sentence prettier at the cost of being a second interpretation of a column
    # whose entire purpose is to be the source's own words. Nothing in the tool parses
    # ``position_raw`` — an analyst does, against the integrated coordinates — and a
    # verbaliser that reshaped it would be the one exception.
    raw = _cell(row, "position_raw")
    return None if raw is None else str(raw)


def _bound_text(row: Mapping[str, Any] | pd.Series, which: str) -> str | None:
    """One temporal bound's display text (``which`` is ``lower`` or ``upper``)."""
    unix_us = _cell(row, f"time_{which}_unix_utc_us")
    if unix_us is not None:
        return render_local(int(unix_us), _cell(row, f"offset_to_local_hours_{which}"))
    for suffix in _BOUND_FALLBACK_SUFFIXES:
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


def _bearer(row: Mapping[str, Any] | pd.Series) -> str | None:
    """The bearer's text when ANOTHER entity holds the position, else None.

    ``subject`` rows repeat the targeted entity as their bearer (decision 55), so naming it
    again would read "Entity X was measured at X"; the place is the object there instead. A
    typed bearer with no identifier is still named by its type — it exists in the record.
    """
    if _cell(row, "entity_link_matlas") == V.EntityLink.SUBJECT.value:
        return None
    bearer = _cell(row, "bearer_entity") or _cell(row, "bearer_raw_identifier")
    if bearer is not None:
        return str(bearer)
    bearer_type = _cell(row, "bearer_entity_type")
    if bearer_type is not None and bearer_type != V.EntityType.UNKNOWN.value:
        return f"an unidentified {bearer_type}"
    return None


def _temporal_clause(link: str | None, timespan: tuple[str, bool]) -> str:
    """The temporal clause: the link's interval or instant fragment, then the timespan.

    Without a link an instant reads "at T" and an interval "during the interval from A to B"
    — neutral glue that asserts no quantifier.
    """
    body, is_instant = timespan
    if link is None:
        connector = V.TemporalKind.INSTANT.phrase if is_instant else "during"
    else:
        member = V.TemporalLink(link)
        connector = member.instant_phrase if is_instant else member.phrase
    return f"{connector} {body}"


def verbalize(row: Mapping[str, Any] | pd.Series) -> str:
    """Render one observation (a flat-view row) as one deterministic sentence.

    ``row`` is a flat-view row: a pandas Series (one row of
    ``flat_view.build_flat_view``'s frame) or any dict-like mapping of column name to
    value — absent columns and NA cells both read as "no recorded value".
    """
    subject = _subject(row)
    place = _place(row)
    timespan = _timespan(row)
    bearer = _bearer(row)
    spatial_phrase = _link_phrase(row, "spatial_link_matlas", V.SpatialLink)
    temporal_link = _cell(row, "temporal_link_matlas")

    # The spatial clause. With a verb, its object is the bearer when another entity holds the
    # position (the place, or its said absence, follows) and the place otherwise. A spatial
    # expression the integration could not place still keeps its verb — "searched for <poi>"
    # stays true even when no coordinate was resolved. With no verb at all, a bearer is
    # introduced by the entity-link phrase, which asserts no use of a position.
    if spatial_phrase is not None and bearer is not None:
        where = f"at {place}" if place is not None else NO_POSITION_WORDING
        spatial = f"{subject} {spatial_phrase} {bearer}, {where}"
    elif spatial_phrase is not None and place is not None:
        spatial = f"{subject} {spatial_phrase} {place}"
    elif place is not None:
        spatial = f"{subject} {_UNLINKED_POSITION_WORDING} {place}"
    elif bearer is not None:
        spatial = f"{subject}, {V.EntityLink.PARTY.phrase} {bearer}, {NO_POSITION_WORDING}"
    else:
        spatial = f"{subject}, {NO_POSITION_WORDING}"

    # An empty temporal corner is SAID; the quantifier has nothing to quantify, so it drops.
    if timespan is None:
        joiner = " and" if place is None else ","
        return f"{spatial}{joiner} {NO_TIME_WORDING}."
    return f"{spatial}, {_temporal_clause(temporal_link, timespan)}."
