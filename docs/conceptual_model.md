# Conceptual model — what mATLAS records

Status: **settled at the conceptual level (2026-07-11)**, before implementation. This
document describes the world-model; it deliberately contains no tables, no SQL, no ids.
The implementation mapping (SQLite schema, id scheme) lives in
[run_db_schema.md](run_db_schema.md) and must justify itself
against this document. The identity discipline it must follow is
[identity_model.md](identity_model.md). The entity corner is elaborated in
[entity_model.md](entity_model.md); the 2026-07-11 review record is
[model_review_findings.md](model_review_findings.md).

## The model in one sentence

> **A record claims that one or more participants relate to a place at a time.
> Every corner is stored in the source's own words; every normalisation the tool
> performs is attached to the corner, never substituted for it.**

The entity–space–time triangle survives as the shape of a claim. What this model fixes
is the *inside* of each corner: the previous model stored what the source expressed and
what the tool computed from it in the same slot, with the same identity — so a geocoded
latitude was indistinguishable from a latitude the source itself stated, and two
different phones labelled `device` became one entity.

## The four layers

### Layer 0 — Acquisition (immutable, never merged)

- **Source** — one reading of one acquired artefact (an exhibit occurrence). Two
  byte-identical artefacts are **two** sources whose matching hash is a *finding*,
  never a reason to merge (see identity_model.md).
- **Record** — one verbatim unit within a source. Preserved complete and untransformed.

### Layer 1 — Claim (what the source says, in its own vocabulary; immutable)

A **claim** is extracted from exactly one record. It carries:

- **Participants (1..N), each with a role.** A participant is a **mention**: the
  identifier *as written* (`+33612…`, `device`, a name on a booking) plus its kind
  (msisdn, imei, email, descriptive label, plate, account, …). Roles come from a closed
  vocabulary: `subject`, `caller`, `callee`, `sender`, `recipient`, `reporter`,
  `counterpart`, … A mention is an occurrence, not a resolved thing: the model never
  infers that two equal strings denote the same real-world object — that inference is
  an explicit Layer-2 act.
- **A place, as expressed.** A **spatial expression** with a *kind*: direct
  coordinates, address, place name, cell/antenna reference, region, or none. It also
  carries **whose position it is** (`locates`): `self` (GPS fix — the subject located
  itself), `infrastructure` (antenna, ANPR camera, merchant terminal — the subject was
  *near* it), or `referenced` (a shared map pin — nobody was necessarily there).
  `locates` is *derived* from the entity–position link, never stored, so the two can
  never contradict (run_db_schema.md, decision 11).
- **A time, as expressed.** A **temporal expression**: the raw value(s), the stated
  calendar/offset (or the fact that none was stated), the granularity, and whether it
  is an instant, an interval, or an open bound.
- **The link vocabularies** (entity–position, entity–time, spatial–temporal) and an
  **epistemic qualifier**: `measured` | `recorded` | `claimed` | `reported` |
  `negated`. Metric accuracy is instrument error and cannot express "the witness
  thinks so"; the qualifier can. *Implementation (owner decision, 2026-07-11):
  the qualifier is not a separate column — the epistemic and confidence weight is
  carried inside the link vocabularies themselves (`CLAIMED_AT`, `REPORTED_FOR`,
  `NEVER_DURING_INTERVAL`, …), completed as new source types require
  (run_db_schema.md decisions 6 and 13).*

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

**A line is a placement** (run_db_schema.md decisions 14 and 18): a party earns its
own line only when the record gives it its own placement; every other party the
record names attaches to an existing line as a *context mention* and appears as a
column, never a row.

- a GPS fix → one line (subject = the device, locates = self);
- a one-legged CDR → **one line** (caller at antenna X; the callee and the caller's
  IMEI are context mentions — counterpart and facet columns, **no invented
  position**);
- a record disclosing both legs' cells → two lines on one activity, each leg with its
  own position; two *different records* of one call (two operators) are linked later
  by an explicit `activity_link` analysis act — v1 records them as two activities and
  promises no automatic pairing;
- an ANPR read / witness statement / police register entry → one line (the detector
  and the reporter are context; a register's event reference IS the entity itself —
  type `event`, identifier == concerns, decision 36);
- a claim with no placement at all (a login) → a line without coordinates, never
  dropped.

Each line carries its role, its facets, its counterpart(s), its activity, and its
record reference, so the fan-out no longer destroys the event structure — and the
line count equals the placement count, which is the grain an examiner exploits.
The activity is a grouping bonus only (decision 35): anything exploitable
spatio-temporally lives in the entity, the links and the roles, never only in the
activity — a leg's counterpart is the other assertion sharing its `activity_id`.

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

## Documented v1 choices (limitations, not model changes)

1. **One preferred interpretation per expression** is materialised; the model allows
   plural, v1 stores the chosen one (with its method) and may add alternatives later.
2. **WGS84 latitude/longitude** is the chosen geometry form for all spatial kinds.
3. **UTC microseconds** is the chosen temporal position form.
4. Closed vocabularies (expression kinds, roles, locates, epistemic qualifiers) are
   enumerated at implementation in `families.py`, which stays the single declaration.
5. **Negative claims** are representable (`negated`) but no v1 source produces them.
6. **Event grouping** is defined now, populated when a multi-leg source (CDR) ships.
7. **Identity resolution** ships with one cheap deterministic method (identifier-kind
   mentions with equal kind+value resolve to one identity); descriptive labels are
   never auto-resolved. Cross-identifier resolution (one person, two numbers) stays a
   later, reviewable layer.
