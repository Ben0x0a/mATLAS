# Entity model — the who-stack

The entity side of the model: what carries an identity, how identifiers are normalised, and the
analyst layer that is deferred out of the current version. It elaborates the entity corner that
[conceptual_model.md](conceptual_model.md) introduces and that [schema.md](schema.md) implements.

## The stack, bottom to top

| tier | object | key kind | who writes it |
| --- | --- | --- | --- |
| L1 | **mention** (`entity`: value as written + type) | occurrence | ingest |
| L1 | **facets / context** (`assertion_mention`: the caller's IMEI, the counterpart, the detector…) | pure link | ingest |
| L2 | **canonical form** (`entity_integration`: E.164, lowercase email…, private 1:1, method-stamped) | occurrence | ingest (methods) |
| L3 | **nodes** (`resolved_entity`) + **relations** (`entity_relation`) | see below | operator / analysis / sanctioned ingest |
| L3 | **resolution acts** (`mention_resolution`) | occurrence | operator / analysis only |

Sameness never happens by row sharing: equal canonical values converge in queries
(`GROUP BY canonical_kind, canonical_value`); anything stronger is an explicit,
recorded act.

## Scoped canonicalizability

`EntityType.canonicalizable` is not a boolean in practice — it is a *rule per kind*,
and the canonicalization method may **refuse** (emit no integration row) rather than
guess. Refusal is honest: a mention without a canonical form simply never converges.

| kind | rule |
| --- | --- |
| msisdn | E.164; requires country context (preset default region or a leading `+`) — otherwise **refuse** |
| imei | 14-digit body normalised (check digit dropped, recorded in method parameters) |
| imsi, email, plate | global canonicalisation (plate: country-qualified, spacing/dash stripped) |
| account | canonical **only with a service qualifier**: `value = <service>:<handle>` (`telegram:@x`); a bare handle **refuses** |
| ip | public ranges only; private/CGNAT ranges (10/8, 192.168/16, 100.64/10…) **refuse** — equal private IPs on two networks are not the same endpoint |
| mac | universally-administered only; locally-administered / randomised bit set → **refuse** |
| cell, camera, bssid, terminal | canonical within their register namespace (`mcc-mnc-lac-ci`, register asset id) |
| case_ref | canonical within the issuing system: `value = <issuer>:<ref>` |
| device, label, person_name, unknown | **never** — descriptive, nothing to converge |

## Two node classes

`resolved_entity` holds two different things, and their identity differs:

- **Identifier nodes** (kind ∈ canonicalizable kinds): content-keyed —
  `uuid5(kind ⟂ canonical value)`. Value-equality genuinely is identity here; this is
  the model's only sanctioned content key.
- **Individual nodes** (kind = person | organization): **occurrence-keyed** —
  `uuid5(designation ⟂ creating act)` where the creating act is the operator/analysis
  input that minted the node (its origin + parameters + ordinal, so a replayed run is
  still diffable). Two "John Smith"s are two nodes **unless the operator merges them
  explicitly  attribution-merge defect cannot recur at the attribution layer.

A `CHECK` prevents individual kinds from entering by the content-key path; the store
exposes two distinct write calls.

## Relations carry provenance

`entity_relation` columns: `subject_id`, `object_id`, `relation_type`
(used_by | subscribed_by | owned_by | **alias_of** | **registered_to** | …),
`valid_from`, `valid_to` (**INTEGER unix µs** — same type as assertion time, so the
validity-covers-time join actually works), `origin`
(operator | preset | **evidence**), `asserted_by` (agent string), `asserted_at`,
and `raw_record_id` (nullable FK — **mandatory when origin = evidence**).

**The sanctioned ingest path**: a record whose *content is itself a relation* — a
contacts row ("Mum" = +44…), a SIM registration, a vehicle register — may be mapped
by a preset directly into `entity_relation`, always citing its `raw_record_id`. This
is the only L3 table ingest may touch, and only through that citation; *derived value
⇒ documented derivation* holds because the record is the derivation.

## Resolution acts (`mention_resolution`)

`mention_resolution(mention_resolution_id, entity_id → mention, resolved_entity_id,
method, origin, UNIQUE(entity_id, method))` — the explicit act "this mention denotes
that node": the witness's "him" → a person node; a bare device → the exhibit's owner;
a hotel-register name → an identified guest. One verdict per (mention, method);
a competing verdict is a new row, never an overwrite. Defined now, populated by
operator/analysis only — never at ingest.

## What produces the CSV `concerns`

Unchanged by this document: one concerns value per assertion, fixed at
parsing, `concerns_origin` recorded). The relation graph answers *analyst*
questions ("whose number was this in March?"); it never produces the CSV column.
Possible future unification of the decision-12 FK with `assertion_mention`
(role=owner) is recorded as an option, not done.
