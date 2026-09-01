"""The revised observation model's acceptance test: can it say what it must say?

Defines:    one parametrised encoding of every worked example the model revision was argued
            on (the fifteen rows of the plan's §5), the four spatial x temporal combinations,
            and the two invariants that keep the corners from re-conflating.
Used by:    nothing — this is a guard, not a library.
Depends on: ``model_atlas.core.vocabularies`` (the vocabularies under test) and
            ``model_atlas.core.ids`` (the L1 projection, to prove distinct claims stay
            distinct).

WHY this file exists: the decomposition of the old ``EntityPositionLink`` into three
orthogonal corner links plus ``attestation`` was justified on a specific table of real
sources. If a future edit collapses two of those axes back together, the enums will still be
valid and the DDL will still build — only these encodings will stop being expressible. So
the table IS the test.
"""
from __future__ import annotations

import pytest

from model_atlas.core import ids
from model_atlas.core.vocabularies import (
    Attestation,
    EntityLink,
    Occupancy,
    SpatialLink,
)

# (label, entity_link, spatial_link, occupancy, attestation, temporal_kind)
#
# ``temporal_kind`` is load-bearing, not decoration: at the level of the four link fields
# alone an EXIF stale-cache row is INDISTINGUISHABLE from a GNSS fix (both self / at / once /
# instrument). What separates them is the temporal corner — a GNSS fix is an instant, the
# stale cache is the open-lower interval "(open, t0)", i.e. the device was there at some
# point BEFORE the photo. That is exactly what earns ``once`` its place: without it the only
# options are asserting presence at t0 (false) or dropping the row (loses evidence).
WORKED_EXAMPLES = [
    ("GNSS fix", EntityLink.SELF, SpatialLink.AT, Occupancy.ONCE, Attestation.INSTRUMENT, "instant"),
    ("IP geolocation", EntityLink.SELF, SpatialLink.WITHIN, Occupancy.ONCE,
     Attestation.THIRD_PARTY_REGISTER, "instant"),
    ("CDR leg", EntityLink.SERVED_BY, SpatialLink.NEAR, Occupancy.CONTINUOUS,
     Attestation.SYSTEM_RECORD, "interval"),
    ("ANPR read", EntityLink.SEEN_BY, SpatialLink.NEAR, Occupancy.ONCE,
     Attestation.INSTRUMENT, "instant"),
    ("witness sighting", EntityLink.SEEN_BY, SpatialLink.NEAR, Occupancy.ONCE,
     Attestation.PERSON, "interval"),
    ("ATM withdrawal", EntityLink.USED_AT, SpatialLink.AT, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("card, chain merchant", EntityLink.USED_AT, SpatialLink.WITHIN, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("hotel stay", EntityLink.STAYED_AT, SpatialLink.AT, Occupancy.UNKNOWN,
     Attestation.SYSTEM_RECORD, "interval"),
    ("nav search", EntityLink.SEARCHED_FOR, SpatialLink.REFERENCES, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("chat pin received", EntityLink.SHARED, SpatialLink.REFERENCES, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("alibi statement", EntityLink.SELF, SpatialLink.ABSENT, Occupancy.CONTINUOUS,
     Attestation.PERSON, "interval"),
    ("cache, connection-backed", EntityLink.SERVED_BY, SpatialLink.NEAR, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("cache, prefetched", EntityLink.CACHED, SpatialLink.UNKNOWN, Occupancy.ONCE,
     Attestation.THIRD_PARTY_REGISTER, "instant"),
    ("Waze trace", EntityLink.UNKNOWN, SpatialLink.UNKNOWN, Occupancy.ONCE,
     Attestation.SYSTEM_RECORD, "instant"),
    ("EXIF stale cache", EntityLink.SELF, SpatialLink.AT, Occupancy.ONCE,
     Attestation.INSTRUMENT, "open_lower"),
]


def _links(entity_link, spatial_link, occupancy, attestation):
    return ids.ObservationLinks("unknown", entity_link, spatial_link, occupancy, attestation)


def _project(entity_link, spatial_link, occupancy, attestation, temporal_kind="instant"):
    """One claim's L1 string. The bearer follows the link, exactly as ingest must."""
    bearer = (ids.EntityProjection("228-01-2042-77341", "cell", "antenna")
              if entity_link.needs_bearer else None)
    return ids.l1_projection(
        entity=ids.EntityProjection("+41791234567", "msisdn", "device"),
        bearer=bearer, spatial=None,
        temporal=ids.TemporalProjection(temporal_kind, "iso8601", ()),
        links=_links(entity_link, spatial_link, occupancy, attestation),
        deleted=None, multiplicity=None, activity=None)


@pytest.mark.parametrize(
    "label,entity_link,spatial_link,occupancy,attestation,temporal_kind", WORKED_EXAMPLES,
    ids=[e[0] for e in WORKED_EXAMPLES])
def test_every_worked_example_is_encodable(
        label, entity_link, spatial_link, occupancy, attestation, temporal_kind) -> None:
    """Each row of the justifying table must encode with no field doing two jobs."""
    assert _project(entity_link, spatial_link, occupancy, attestation, temporal_kind)


def test_every_worked_example_is_a_distinct_claim() -> None:
    """No two of the fifteen collapse onto one digest.

    If a future edit folds two axes back together, distinct real-world claims start hashing
    equal — which the duplicate-evidence machinery would then report as possible
    corroboration. That is the failure this whole decomposition exists to prevent.
    """
    digests = {}
    for label, *cells in WORKED_EXAMPLES:
        digest = ids.claim_digest(_project(*cells))
        assert digest not in digests, f"{label!r} collides with {digests[digest]!r}"
        digests[digest] = label


@pytest.mark.parametrize("label,a,b", [
    # Same verb, same attestor, same occupancy — ONLY the spatial link differs. This is the
    # contrast that forced spatial_link to be STORED rather than derived from the verb: the
    # granularity of the geometry is not recoverable from the interaction.
    ("self at a point vs within a region",
     (EntityLink.SELF, SpatialLink.AT, Occupancy.ONCE, Attestation.INSTRUMENT),
     (EntityLink.SELF, SpatialLink.WITHIN, Occupancy.ONCE, Attestation.INSTRUMENT)),
    ("used_at an ATM vs a chain merchant",
     (EntityLink.USED_AT, SpatialLink.AT, Occupancy.ONCE, Attestation.SYSTEM_RECORD),
     (EntityLink.USED_AT, SpatialLink.WITHIN, Occupancy.ONCE, Attestation.SYSTEM_RECORD)),
    # Same verb, same spatial link — ONLY the attestor differs. An ANPR camera and a witness
    # both "saw" the entity near the same place; attestation is the only field that can carry
    # the difference, which is why it could not be folded into the verb.
    ("ANPR camera vs witness, both seen_by",
     (EntityLink.SEEN_BY, SpatialLink.NEAR, Occupancy.ONCE, Attestation.INSTRUMENT),
     (EntityLink.SEEN_BY, SpatialLink.NEAR, Occupancy.ONCE, Attestation.PERSON)),
    # Same everything — ONLY the verb differs, and it changes what may be claimed spatially.
    ("harvested vs prefetched almanac",
     (EntityLink.SERVED_BY, SpatialLink.NEAR, Occupancy.ONCE, Attestation.SYSTEM_RECORD),
     (EntityLink.CACHED, SpatialLink.NEAR, Occupancy.ONCE, Attestation.SYSTEM_RECORD)),
    # Same everything — ONLY occupancy differs: the alibi vs "left at some point".
    ("absent throughout vs absent once",
     (EntityLink.SELF, SpatialLink.ABSENT, Occupancy.CONTINUOUS, Attestation.PERSON),
     (EntityLink.SELF, SpatialLink.ABSENT, Occupancy.ONCE, Attestation.PERSON)),
])
def test_each_field_carries_a_difference_no_other_field_can(label, a, b) -> None:
    """Minimal contrasts: vary ONE field, hold the rest fixed, demand a distinct claim.

    This is the real independence argument. Comparing whole rows of the worked table proves
    less than it appears to, because two rows typically differ in several fields at once.
    """
    assert sum(1 for x, y in zip(a, b) if x is not y) == 1, "not a minimal contrast"
    assert ids.claim_digest(_project(*a)) != ids.claim_digest(_project(*b))


def test_all_four_polarity_quantification_combinations_are_reachable() -> None:
    """Polarity on space x quantification on time — four statements, not one lumped token.

    ``absent`` + ``once`` ("left at some point") is the one the previous model could not
    express at all: negation was a single token duplicated across two enums.
    """
    combos = [(SpatialLink.AT, Occupancy.CONTINUOUS), (SpatialLink.AT, Occupancy.ONCE),
              (SpatialLink.ABSENT, Occupancy.CONTINUOUS), (SpatialLink.ABSENT, Occupancy.ONCE)]
    digests = {ids.claim_digest(_project(EntityLink.SELF, sl, occ, Attestation.PERSON))
               for sl, occ in combos}
    assert len(digests) == 4


def test_prefetched_cache_asserts_no_device_position() -> None:
    """Condition 2's failing case, and the reason for the whole revision.

    A prefetched almanac row must not claim the device was anywhere. The verb says the device
    only stored the place as data, and the spatial link says nothing is determinable about
    the device's relation to it — so no presence reaches a "who was here" query.
    """
    entity_link, spatial_link = EntityLink.CACHED, SpatialLink.UNKNOWN
    assert not spatial_link.is_presence
    assert entity_link.needs_bearer          # the AP still holds a real position of its own
    # and it stays distinguishable from the harvested row, which DOES place the device
    assert SpatialLink.NEAR.is_presence


def test_bearer_invariant_holds_in_both_directions() -> None:
    """``self`` alone means no bearer; every other verb means exactly one."""
    assert not EntityLink.SELF.needs_bearer
    assert all(link.needs_bearer for link in EntityLink if link is not EntityLink.SELF)
