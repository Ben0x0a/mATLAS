# Conceptual model — what mATLAS records

This document describes the world-model; it deliberately contains no tables, no SQL, no ids.
The implementation mapping (SQLite schema, id scheme) lives in [schema.md](schema.md) and
follows this document. The identity discipline it obeys is
[identity_model.md](identity_model.md); the entity corner is elaborated in
[entity_model.md](entity_model.md).

## The model in one sentence

> **A record says that some entity was (or was not) somewhere at some time — naming the
> entity's role, what the record does with the position, and how the interval is
> referenced, and tracing back to the source that says so. Every corner is stored in the
> source's own words; every normalisation the tool performs is attached to the corner,
> never substituted for it.**

### Content and link, per arm (decision 55)

The observation is the centre, and each arm carries two different things — collapsing them
is what made the earlier vocabulary resist qualification. Each link says, at a technical
level, how the observation is tied to that component; none states what the observation is
worth:

| Arm | Content | Link |
| --- | --- | --- |
| **Entity** | **who / what** (+ the bearer) | **role** — subject / party / mentioned / unknown |
| **Spatial** | **where** | **how the position is used** — measured / registered / served / seen / used / stayed / located / searched / shared / cached / unknown |
| **Temporal** | **when** | **how the interval is referenced** — □P throughout / ◇P at least once / □¬P never / ◇¬P not throughout |
| **Source** | **who says so** | the record chain itself (source file, record, preset) — no vocabulary |

The spatial verb alone decides whether the record claims contact with a place: `searched`,
`shared` and `cached` do not.

The entity–space–time triangle survives as the shape of a claim, though "triangle" flatters
it: the observation is a reified **ternary relation**, and its links are qualifiers of the
whole statement, not edges between corners. A ternary relation cannot in general be
decomposed into three binaries without loss, which is why labelling them as edges kept
producing enums that mixed axes.

What this model fixes is the *inside* of each corner: the previous model stored what the
source expressed and what the tool computed from it in the same slot, with the same
identity — so a geocoded latitude was indistinguishable from a latitude the source itself
stated, and two different phones labelled `device` became one entity.

## The four layers

### Layer 0 — Acquisition (immutable, never merged)

- **Source** — one reading of one acquired artefact (an exhibit occurrence). Two
  byte-identical artefacts are **two** sources whose matching hash is a *finding*,
  never a reason to merge (see identity_model.md).
- **Record** — one verbatim unit within a source. Preserved complete and untransformed.

### Layer 1 — Claim (what the source says, in its own vocabulary; immutable)

A **claim** is extracted from exactly one record. It carries:

- **Two entities: the targeted one and the position bearer.** Each is a **mention**: the
  identifier *as written* (`+33612…`, `device`, a name on a booking) plus its kind
  (msisdn, imei, email, descriptive label, plate, account, …).
  - the **targeted** entity is the one under investigation — the one the flat CSV names
    on its line;
  - the **bearer** is the entity whose position the spatial arm states: for a CDR the
    antenna, for an ANPR read the camera, for an ATM withdrawal the terminal. The
    `entity_link` fixes it: `subject` — the entity holds its own position, so it IS the
    bearer; `party` — another entity holds it; `unknown` — no bearer at all, because a
    placeholder would manufacture a presence claim the source never made.

  So **storage orders by whose position it is, while the projection orders by who is under
  investigation**: the CSV line still names the phone, joining `entity` twice, and the
  antenna survives as a column instead of dissolving into a `cell_ref` string. This also
  makes the infrastructure a first-class, convergeable thing — *"which other cards were used
  at this ATM?"* becomes a `GROUP BY` on a normalised identifier rather than a string match
  against a raw component value.

  A mention is an occurrence, not a resolved thing: the model never infers that two equal
  strings denote the same real-world object — that inference is an explicit Layer-2 act.
- **A place, as expressed.** A **spatial expression** with a *kind*: direct
  coordinates, address, place name, cell/antenna reference, region, or none. Whose position
  it is no longer needs deriving: the bearer *is* the answer. What the **spatial link**
  adds is what the record does with that position — a position `measured` for the bearer
  (GNSS, fingerprinting), one a register `registered` (IP geolocation), the place of an
  antenna that `served` the entity, a place `searched` for… It carries no precision (that
  is the geometry's accuracy) and no negation (that is the temporal link).
- **A time, as expressed.** A **temporal expression**: the raw value(s), the stated
  calendar/offset (or the fact that none was stated), the granularity, and whether it
  is an instant, an interval, or an open bound.
- **The three stored links** (`entity_link`, `spatial_link`, `temporal_link`), stated by
  the preset author and therefore marked as enrichment. They feed **no digest**: two
  readings of one record that disagree on a link still converge, and the disagreement stays
  visible.
- **The source link** is the record chain itself — source file, record, preset — with no
  vocabulary. Evidential weight, credibility and admissibility are **court questions and the
  model must not state them**; the intelligence/admiralty grading is deliberately not adopted
  for that reason.

### Layer 2 — Interpretation (derived, attached, re-runnable)

An **interpretation** turns an expression into a normalised value, and names how:
*method, method version, parameters, agent (tool/preset/operator), and any reference
data used* (geocoder, cell-site register with its version/date, tz database). Products:

- spatial expression → **geometry** (coordinates in a named CRS — we choose WGS84);
- temporal expression → **UTC instant/interval** with accuracy;
- mention → **identity** (a resolved real-world thing).

A pass-through ("the source already said WGS84 / UTC") is still an interpretation — a
trivial one — so the rule *derived value ⇒ documented derivation* has no exceptions.
Interpretations may be plural and competing (two geocode candidates, a DST-ambiguous
local time); the evidence layer is untouched when they change. A tower position derived
from the 2023 cell register is a different fact from one derived from the 2025 register.

#### Conversion vs convergence — why integration rows are never shared

Spatial/temporal integrations *convert* (one expression → a derived reading, possibly
several candidates); entity integration *converges* (many mentions → the same canonical
value). This looks like an asymmetry but is governed by one rule: **an integration row
is always private to its expression; convergence happens by canonical-value equality or
by an explicit resolution layer — never by sharing rows.** Whether value-convergence
works depends on the value space: identifiers and UTC instants are discrete, so exact
equality converges them (a `GROUP BY`); coordinates are continuous, so "same place
across formats" is unreliable as value equality and belongs to a future *resolved
place* layer (the spatial twin of the resolved-entity relation graph) — an explicit,
reviewable act, exactly like entity resolution. The asymmetry is in the world
(symbols vs geometry), not in the schema.

### Layer 3 — Analysis (statements *about* claims; never inside them)

Everything the tool or analyst concludes on top of the evidence: untangle
rank/record-type, duplicate-evidence findings, operator-asserted attribution
(MSISDN → person), and **event grouping** — the statement that several claims are legs
of one event (the two ends of a call, the two airports of a flight, the fixes of one
trip). Analysis is re-derivable, versioned, and separately attributed; deleting it
loses no evidence.

## Identity principle

Occurrences (source, record, claim, mention, expression, interpretation) get
surrogate occurrence keys and are **never merged**. Content-addressing is permitted
only where value-equality genuinely is identity — and after this model, *no ingest-time
table has that property*: even entity deduplication is an explicit interpretation
(mention → identity), not a side effect of equal strings. Full discipline:
[identity_model.md](identity_model.md).

## The flattening rule (relational → CSV)

**A line is a placement** (see schema.md): a party earns its
own line only when the record gives it its own placement; every other party the
record names attaches to an existing line as a *context mention* and appears as a
column, never a row.

- a GPS fix → one line (targeted = the device, `entity_link = subject`, its own bearer);
- a one-legged CDR → **one line** (caller at antenna X; the callee and the caller's
  IMEI are context mentions — counterpart and facet columns, **no invented
  position**);
- a record disclosing both legs' cells → two lines on one activity, each leg with its
  own position; two *different records* of one call (two operators) are linked later
  by an explicit `activity_link` analysis act — v1 records them as two activities and
  promises no automatic pairing;
- an ANPR read → one line (targeted = the plate, **bearer = the camera**, which now has a
  real row instead of being dissolved into an asset reference); a witness statement → one
  line traced to the statement file; a police register entry → one line whose event reference
  IS the entity itself (type `event`, identifier == concerns);
- a claim with no placement at all (a login) → a line without coordinates, never
  dropped.

Each line carries its role, its facets, its counterpart(s), its activity, and its
record reference, so the fan-out no longer destroys the event structure — and the
line count equals the placement count, which is the grain an examiner exploits.
The activity is a grouping bonus only): anything exploitable
spatio-temporally lives in the entity, the links and the roles, never only in the
activity — a leg's counterpart is the other observation sharing its `activity_id`.

## Validation — the source types this model was tested against

| Source | What exercises the model |
| --- | --- |
| Phone GPS/location caches, trackers | the native fit: self-located, coordinates + epoch |
| CDR / tower dumps | roles (caller/callee), cell-ref expression, infrastructure-located, register lookup as interpretation, event grouping |
| ANPR | plate = mention (maybe cloned/misread), camera-located, OCR confidence |
| Card transactions | address expression → geocode interpretation; card-not-present = no position |
| Hotel/airline registries | interval semantics, place names, two places in one record (journey) |
| Wi-Fi / IP logs | IP→geo as a *weak* interpretation, named and confidence-classed |
| EXIF photos | offset-less local time, camera-located, contested integrity |
| Witness statements | reporter role, unresolved mention ("him"), fuzzy place/time, `reported`/`negated` |
| Chat shared locations / check-ins | `referenced` position — coordinates nobody was at; sender/recipient roles |

## The model's own acceptance test

A revision of this model is judged on four conditions. They are stated here so a future
change is argued against them rather than against taste:

1. **Can it encode all the location data we can think of?**
2. **Does it encode it well** — are the key characteristics kept?
3. **Can it be reasoned on** — tested the practical way: *from a database export, is
   everything needed to do the job there?* Not whether a module happens to exist.
4. **Is the information source explicit** — is what came from the source distinguishable
   from what the engine derived?

Condition 2 is the one that bites. It is not satisfied by losing nuance gracefully; it is
violated outright when the model *manufactures* a claim. The worked case: a prefetched
location-cache row (a device holding an almanac tile it was told about, not one it saw)
must not assert device presence. `entity_link = party` with `spatial_link = cached` is
what makes that expressible — the tile holds the position, the device only stored it, and
any "plausibly within range" inference becomes an explicit, attributed, deletable analysis
act. Likewise a Waze trace of unknown nature is `entity_link = unknown` with no bearer, never
a placeholder that would read as presence.

### Direction: the two corner algebras

Time and space get matching treatment, one stored quantifier plus one derived relation
family:

| | Stored | Derived at query time |
| --- | --- | --- |
| **Temporal** | interval `[lower, upper]` + `temporal_link` (□/◇, ±) | **Allen's interval algebra**, the 13 relations |
| **Spatial** | region (point / disc / wedge / polygon) + `spatial_link` | **RCC-8** / DE-9IM |

Neither relation family is ever stored — both are computable from the bounds and the
geometry. Note the model is already half-way to regions without saying so: `beam_azimuth_deg`
and `beam_width_deg` are wedge parameters, while `spatial_integration` stores only a point
plus `horizontal_accuracy_m`, i.e. a disc. A cell sector is a wedge, not a disc. **Region
geometry is a later pass**; the vocabulary is only kept from precluding it.

## Documented v1 choices (limitations, not model changes)

1. **One preferred interpretation per expression** is materialised; the model allows
   plural, v1 stores the chosen one (with its method) and may add alternatives later.
2. **WGS84 latitude/longitude** is the chosen geometry form for all spatial kinds.
3. **UTC microseconds** is the chosen temporal position form.
4. Closed vocabularies (expression kinds, the three stored links) are enumerated at
   implementation in `core/vocabularies.py`, which stays the single declaration.
   `SpatialLink` is deliberately an **open list**: a verb is added when a real source needs
   one, then the list is reviewed to minimise it (decision 55).
5. **Negative claims** are representable as `temporal_link = never` (□¬P, an alibi) or
   `not_throughout` (◇¬P, "left home at some point"), and negation lives on the temporal
   link *only*. This is forced, not stylistic: modal duality gives ¬◇P ≡ □¬P, so negation
   storable on two arms would let one fact be written two ways — exactly the bug
   `never_during_interval` caused by existing on two enums at once. A negation needs the
   source to STATE it; a missing record is never one.
6. **Event grouping** is defined now, populated when a multi-leg source (CDR) ships.
7. **Identity resolution** ships with one cheap deterministic method (identifier-kind
   mentions with equal kind+value resolve to one identity); descriptive labels are
   never auto-resolved. Cross-identifier resolution (one person, two numbers) stays a
   later, reviewable layer.
