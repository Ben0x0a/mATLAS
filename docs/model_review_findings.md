# Model review findings — senior review of the run-database model

Status: **review record, 2026-07-11; second review appended 2026-07-12.** A four-seat
review (ontology, forensic practice, data engineering, adversarial) of
[conceptual_model.md](conceptual_model.md)
and [run_db_schema.md](run_db_schema.md), performed before any code. The second
review (R27–R42, below the first statute table) re-audited the updated model after
decisions 12–15, Phase 0 and the demonstration database. Each finding
names its proposed change and its state: **owner-decided**, **open decision**
(needs the owner), or **pin-down** (no model change — a rule the docs must state).
The entity-side findings converge on one redesign, proposed separately in
[entity_model.md](entity_model.md).

## Owner decisions taken on this review

1. **Confidence and epistemic weight live in the three link vocabularies** on the
   assertion (`entity_position_link`, `entity_time_link`, `spatial_temporal_link`),
   not in dedicated columns. Numeric source-stated confidences (an ANPR OCR score)
   remain available verbatim in `raw_record.raw_json` / the `orig_` passthrough.
   Recorded as decision 13 in run_db_schema.md. Consequence accepted: completing
   the link vocabularies (open item 1) carries this weight and must cover the
   reported/negated spatial cases when those sources ship.

## Blockers

### R1 — The identity/attribution joint is unreachable or silently wrong

Four findings compose into one failure chain (all seats):

- A bare device mention (`value NULL`, type `device`) is non-canonicalisable by
  design, so it can never soft-join the analyst graph — yet it is the most common
  mention in a handset extraction. *Partly resolved by decision 12: the CSV
  `linked_entity` is now the per-assertion linked mention fixed at parsing, so the
  attribution contract no longer depends on the graph. The graph-side gap remains
  for analyst queries.*
- No table records a mention→identity act. The witness's "him", a hotel guest's
  name, a bare device: no operator act, however explicit, has anywhere to land.
- `entity_relation.valid_from/valid_to` are TEXT while assertion time is INTEGER;
  SQLite orders INTEGER below TEXT **always**, so any validity-covers-time query
  is constant-false — silently empty, no error.
- Where canonical equality does fire it is too strong: private IPs, service-less
  account handles and randomised MACs converge, fabricating identity.

**Change:** the entity-stack refinement in [entity_model.md](entity_model.md)
(scoped canonical identity, two node classes, typed validity bounds), built on
decisions 3, 7, 8 and 12. **Open decision.**

### R2 — `resolved_entity` content-keys persons

`uuid5(kind‖value)` with kind=person merges two distinct people who share a
designation ("John Smith"), silently, via SELECT-then-INSERT — the §2d defect
readmitted at the attribution layer, against the model's own rule that content
keys are permitted only where value-equality genuinely is identity.
**Change:** occurrence-keyed individual nodes (part of entity_model.md).
**Open decision.**

### R3 — The stated storage mode does not fit the stated workload

Fully in-memory SQLite with verbatim `raw_json` and TEXT uuid keys costs roughly
35–80 GB at 10–20 M records (a realistic CDR dump or UFDR chat store) — beyond
analyst hardware. **Change:** open the store as a *temp on-disk* database
(`journal_mode=OFF`, `synchronous=OFF` — the run DB is reproducible scratch,
crash-safety of the scratch file is not a fidelity property) and persist with
`VACUUM INTO '<name>.matlas.sqlite'`, which also removes the `backup()` API and
with it the autocommit/backup-deadlock constraint entirely. Optional second
lever: 16-byte BLOB uuids. **Open decision** (semantics unchanged either way).

## Major

### R4 — Evidence that *is* a relation has no legal write path

A contacts row ("Mum" = +44…), a SIM-registration record, a vehicle-registration
file: the record's content is a standing relation between identifiers, but ingest
is forbidden from writing the graph and `entity_relation` cannot cite a record.
`RelationOrigin.EVIDENCE` exists with no way to honour "derived value ⇒ documented
derivation". **Change:** provenance columns on `entity_relation` (`asserted_by`,
`asserted_at`, nullable `raw_record_id` FK — mandatory when origin=evidence) and a
sanctioned ingest path for record-borne relations; extend `RelationType`
(alias_of, registered_to). Part of entity_model.md. **Open decision.**

### R5 — Duplicate-evidence detection misses the routine case

`content_hash` embeds `fingerprint_sha256 ⟂ line_no ⟂ output_ordinal`, so two
overlapping CDR disclosures (different files, 3 000 shared records) never
converge: duplication is presented as corroboration, and the duplicate query
reports "none". A preset fix that shifts one ordinal likewise breaks cross-run
comparison. **Change:** add a second, file-independent digest over the L1
projection alone (no fingerprint, no line_no, no ordinal), indexed, projected as
a *possible same-evidence* finding — hash-equality-as-finding is already the
design's philosophy. Document `content_hash` as a claim digest scoped to the
preset's reading. **Open decision.**

### R6 — The founding defect resurfaces in the flat CSV

The database now distinguishes a stated coordinate from a geocoded one, but the
flat view projects one lat/long pair; `locates` is derived-and-never-stored,
integration `method` is not in the column list, and the referentials are listed
as additions "if wanted". The CSV — the artefact examiners actually read —
renders a GPS fix, a geocoder centroid and a cell-register lookup identically.
**Change:** make `spatial_referential`, `locates` and the integration `method`s
mandatory flat columns. **Open decision** (column list only, no schema change).

### R7 — Activity↔record integrity is unenforceable

`activity_id` derives from `raw_record_id ⟂ activity_ordinal`, but the table has
no `raw_record_id` column; uuid5 is one-way, so a derivation bug attaching an
assertion to a neighbouring record's activity raises nothing and the flat
`counterpart` column attributes a call leg to the wrong call. **Change:** add
`activity.raw_record_id FK NOT NULL` + `UNIQUE(activity_id, raw_record_id)` and
make the assertion's reference composite —
`FOREIGN KEY (activity_id, raw_record_id) REFERENCES activity(...)`. Cross-record
"same call" stays an analysis act, exactly as designed. **Pin-down.**

### R8 — Cross-record event linking is promised but structurally absent

conceptual_model.md's flagship example (two operators' records of one call
"sharing an event id") is undeliverable: activity ids are per-record and the
"analysis links between activities" step names a table that does not exist.
**Change:** define `activity_link(subject_activity_id, object_activity_id,
relation_type ∈ {same_event, part_of, reports_on}, method, origin)` now, populate
later; align the conceptual-model wording so v1 does not promise it. **Open
decision** (define-now-vs-defer).

### R9 — Determinism invariants are unwritten

Ids and `content_hash` embed `line_no` and `output_ordinal`, yet no rule pins
record enumeration per format (SQLite needs `ORDER BY rowid` / PK order;
WAL/journal-carved records need a stated position after live rows; Excel needs a
blank-row rule), path canonicalisation for `source_id` (absolute, NFC), or the
flat view's `counterpart` aggregation order. A conforming implementation can
satisfy every constraint and still break the diffability promise. **Change:**
a normative enumeration section in run_db_schema.md plus tests. **Pin-down.**

### R10 — Nested containers abort the run

`container_chain` is in neither the source uuid5 input nor the natural UNIQUE.
One outer archive holding `backup1.zip/Manifest.db` and `backup2.zip/Manifest.db`
yields two legitimate sources with identical key inputs → PK collision → the run
aborts on legitimate evidence. **Change:** define `raw_source_path` as the full
chain-inclusive inner path, or add `container_chain` to the key and the UNIQUE.
**Pin-down.**

### R11 — Storage-layer recovery vs content-layer deletion are conflated

`recovery_state` (how the bytes were obtained — live/WAL/journal, Layer 0) and a
live row's own `is_deleted = 1` flag (what the source claims, Layer 1) are
different facts with one column; Cellebrite exports carry both at once, so the
accepted risk in finding F4 dies when UFDR ships. "Was this message deleted by
the user, or recovered by your tool?" must have two answers. **Change:** keep
`recovery_state` at L0; give the deletion claim an L1 representation. **Open
decision.**

### R12 — Infrastructure is not first-class

Cell ids, ANPR camera ids, BSSIDs are discrete symbols — by the model's own
conversion-vs-convergence rule they should converge by canonical equality — but
they exist only inside `spatial_expression.raw_position`, whose integration emits
only WGS84. "Which phones used tower X" is raw-string archaeology, and records
*about* infrastructure (a tower-outage log) force a fabricated mention because
`assertion.entity_id` is NOT NULL and `EntityType` has no infrastructure kinds.
**Change:** canonicalisable infrastructure EntityTypes (cell, camera, bssid…) and
a `DETECTOR`/`INSTRUMENT` EntityRole, letting presets emit the instrument as a
second mention on the activity. Also gives clock-offset/calibration challenges a
target. **Open decision.**

## Minor

- **R13** — `stated_utc_offset_*` as REAL leaks interpretation into L1: a source
  stating "CEST" states a designation, not a number; resolving it is an
  unstamped derivation. Store the stated designation verbatim (TEXT); numeric
  offsets live only in the integration. **Open decision.**
- **R14** — Region/place-name expressions integrate to a WGS84 point: mandate the
  `method` naming convention (`region_centroid/…`) so the substitution is always
  visible; plan a geometry column for the plural-candidates revision. **Pin-down.**
- **R15** — Aggregate records ("137 calls in March") collapse to one assertion
  with the count invisible outside `raw_json`. Add an assertion-level
  multiplicity column (NULL = 1). **Open decision.**
- **R16** — The tool can eat its own output: a persisted run DB or exported CSV
  under `--input` is a discoverable source, laundering L2/L3 back into L0. Stamp
  persisted DBs (SQLite `application_id` + marker table) and refuse or flag them
  at discovery. **Pin-down.**
- **R17** — `fingerprint_sha256` scope is undefined for SQLite with WAL/journal
  siblings; main-file-only fingerprinting can report a *false* "same source
  acquired twice" finding when the WALs differ. Define the covered bytes and
  record per-sibling fingerprints. **Pin-down.**
- **R18** — `raw_json` "COMPLETE source row" needs a BLOB policy before UFDR
  media tables (e.g. `{"$blob": {"sha256": …, "bytes": n}}`), and a canonical
  JSON form if a record-level digest (R5) is adopted. **Open decision.**
- **R19** — No schema/vocabulary version in the persisted artefact: add
  `PRAGMA user_version` + a one-row meta table (schema version, families
  version); ATTACH-aggregation refuses mismatched majors. **Pin-down.**
- **R20** — The ingestion write order "activity → assertion + entity +
  expressions" violates immediate FK checking under autocommit: entities and
  expressions must insert before their assertion. **Pin-down.**
- **R21** — The flat projection cannot be a static SQL VIEW (per-source `orig_`
  column union): specify it as a generated query per run, explode `orig_` per
  source partition, and declare the indexes its joins need (at minimum
  `assertion(activity_id)` — SQLite creates no FK indexes). **Pin-down.**
- **R22** — `assertion.raw_source_path` duplicates a per-source constant with no
  guard; drop it and join `source` (keep `source_label`/`source_tier`, whose
  per-record variance is documented). **Open decision.**
- **R23** — Chain-of-custody references: optional operator-supplied `exhibit_ref`
  per input and examiner identity on `run`. **Open decision.**
- **R24** — `--entity` (operator override of the mention value) fabricates an L1
  mention the source never made — the same duality analysed for `linked_entity`
  in finding F1 was never analysed for `entity`. **Open decision.**
- **R25** — Modality: a booking or calendar entry asserts intent, not occurrence;
  the flat line's meaning "was at this place at this time" over-claims for
  exactly the sources that place suspects at scenes. Owner decision 1 places
  this weight in the link vocabularies — cover it there (a `SCHEDULED_AT`-class
  member) when a booking source ships. **Open decision.**
- **R26** — Flattening-rule wording: conceptual_model.md says a one-legged CDR is
  one line (callee as counterpart only); run_db_schema.md says one line per
  assertion including positionless participants (callee = own line). Both are
  defensible; pick one and align both docs — row counts change meaning either
  way (a 20-recipient group message is 21 assertions). **Open decision.**

## Statute — integration ruling (2026-07-11, with the owner)

Every finding is ruled **integrated** into the refactor plan
([refactor_plan.md](refactor_plan.md)), at one of three dispositions: *docs-now*
(folded into run_db_schema.md / entity_model.md before code), *build* (implemented in
a named plan phase), or *define-now-populate-later* (schema/vocabulary reserved,
populated when the triggering source type ships). Nothing is rejected outright except
one sub-lever noted below. NOTE: `entity_model.md` is referenced by this review and by
conceptual_model.md but was never written — producing it is the first act of Phase 0.

| finding | statute | disposition |
| --- | --- | --- |
| R1 identity/attribution joint | integrated: validity bounds become INTEGER unix-µs; canonicalizability scoped per kind (private IP / randomised MAC / bare handles → NOT canonicalisable); `mention_resolution` (explicit analyst mention→node act) defined now | docs-now (entity_model.md) + Phase 1 schema; resolution populated later |
| R2 persons content-keyed | integrated: two node classes — identifier nodes stay content-keyed, person/individual nodes become **occurrence-keyed** (two "John Smith" never merge; the operator merges explicitly) | docs-now + Phase 1 |
| R3 storage mode | integrated: temp **on-disk** store (journal_mode=OFF, synchronous=OFF) + `VACUUM INTO` persist — dissolves the backup-deadlock constraint. Sub-lever 16-byte BLOB ids **rejected for v1** (debuggability over size) | Phase 1 (store) |
| R4 evidence-borne relations | integrated: `entity_relation` gains `asserted_by`, `asserted_at`, nullable `raw_record_id` FK (mandatory when origin=evidence); sanctioned ingest path; RelationType += alias_of, registered_to | docs-now + Phase 4 |
| R5 overlapping disclosures | integrated: second file-independent digest (`claim_digest`, L1 projection only), indexed, projected as *possible-same-evidence* finding; `content_hash` documented as preset-scoped reading digest | docs-now + Phase 1 (ids) |
| R6 CSV hides derivation | integrated: `spatial_referential`, `locates` (derived), integration `method`s become **mandatory** flat columns | Phase 5 (flat list) |
| R7 activity↔record integrity | integrated verbatim (composite FK) | docs-now + Phase 1 |
| R8 cross-record event link | integrated: `activity_link` defined now, populated later; conceptual_model wording aligned to v1 | docs-now; populate later |
| R9 determinism invariants | integrated: normative enumeration/canonicalisation section + double-run diff test | docs-now + Phase 1 tests |
| R10 nested containers | integrated: `raw_source_path` defined as the **chain-inclusive** inner path (outer!inner/…), so the key needs no extra column | docs-now + Phase 2 |
| R11 recovery vs deletion | integrated: `recovery_state` stays L0; the source's own deletion claim returns as an L1 assertion column (`deleted`) — supersedes cross-check F4's unification, which dies when UFDR ships | docs-now + Phase 2 |
| R12 infrastructure first-class | integrated: canonicalisable infrastructure EntityTypes (cell, camera, bssid, terminal…) + DETECTOR/INSTRUMENT EntityRole; presets may emit the instrument as a second assertion on the activity | Phase 2 (vocab + presets) |
| R13 stated offset is a designation | integrated: `stated_utc_offset_*` become TEXT verbatim (numeric offsets only in the integration) | docs-now + Phase 1 |
| R14 centroid substitution visibility | integrated: normative `method` naming (`region_centroid/…`); geometry column deferred to the plural-candidates revision | docs-now |
| R15 aggregate multiplicity | integrated: nullable `multiplicity` column on assertion (NULL = 1) | Phase 1 |
| R16 self-ingestion guard | integrated: `application_id` + meta table stamp; discovery refuses/flags stamped artefacts | Phase 2 |
| R17 fingerprint scope | integrated: fingerprint = main file bytes; WAL/journal siblings fingerprinted per-sibling and recorded; "same source" finding requires full-set match | docs-now + Phase 2 |
| R18 raw_json BLOB policy | integrated: `{"$blob": {sha256, bytes}}` envelope + canonical JSON form (required anyway by R5) — policy defined now, exercised when UFDR ships | docs-now; UFDR phase |
| R19 schema versioning | integrated: `PRAGMA user_version` + one-row `matlas_meta` table | Phase 1 |
| R20 write order | integrated: corrected order (corners before assertion) in the doc | docs-now |
| R21 flat projection is a query | integrated: projection = generated query per run; FK indexes declared (assertion(activity_id) at minimum) | docs-now + Phase 5 |
| R22 assertion.raw_source_path | integrated: dropped; joined from source | docs-now + Phase 1 |
| R23 chain-of-custody refs | integrated: optional `exhibit_ref` per input + examiner on run | Phase 2 |
| R24 --entity duality | integrated: decision-12 treatment generalised — `entity_origin` column (preset_mapped \| operator_param) beside the mention FK | docs-now + Phase 2 |
| R25 modality | integrated via decision 13: vocabulary members (SCHEDULED_AT-class; INTENDED_FOR already exists) added when a booking source ships | populate later |
| R26 flattening wording | **re-ruled by decision 14 (2026-07-12)**: still one line per assertion, but a party is an assertion only when the record gives it its own placement — context parties (callee without a cell, recipients, detector, reporter, victim, the subject's other identifiers) attach as `assertion_mention` rows and project as columns. A 20-recipient group message is **1 line**; a two-sided CDR is 2, never 3; conceptual_model.md wording to align | docs-now |

## Confirmed strengths — not to be lost in any revision

Occurrence keys everywhere with content hashes demoted to finding-generators; the
expression/integration split with pass-through-as-method; entity-as-mention with
typed-NULL `device`; activity replacing the junction so each call leg owns its
corners; dual stated/applied UTC offsets per bound; `run_id` excluded from child
ids; no invented positions; positionless assertions never dropped; the
silent-loss inventory itself — extend it with one row per finding above as each
is closed.

---

# Second review — re-audit of the updated model (2026-07-12)

> **Supersession note (2026-07-13, decisions 16–20 in run_db_schema.md).** The owner
> simplified the model after this review: the analyst graph (resolved_entity,
> entity_relation, mention_resolution, activity_link) left the v1 schema (decision
> 16), entity became one lean normalized row absorbing linked_entity (decision 17),
> assertion_mention was replaced by raw_json + activity.context_json (decision 18),
> untangle_finding became `analysis` (19), and activity's record FK was restored
> (20). Consequently: **R29 and R35 are moot** (their tables are gone); **R33's
> facet/mention cases dissolve** (context is JSON; the hotel/EXIF party questions
> become context_json content rules); **R34** loses its entity_relation half (the
> assertion UNIQUEs remain to add to the diagram — done 2026-07-13); **R36's**
> envelope shrinks (fewer rows per record); **R27 is resolved in the doc** (the
> normative L1 projection now includes activity.context_json and excludes
> concerns/origins and source_fields); **R32** shrinks (MentionRole and the graph
> vocabularies leave the v1 set). Still live: R27's demo-recipe rewrite, R28, R30,
> R31, R34-remainder, R36 restate, R37–R42.

Status: **review record, 2026-07-12, awaiting the owner's statute pass.** Same
four-seat council (ontology, forensic practice, data engineering, adversarial) plus
a solo pass, over the model as updated by decisions 12–15 and Phase 0:
[run_db_schema.md](run_db_schema.md), [entity_model.md](entity_model.md),
[model_worked_examples.md](model_worked_examples.md),
[matlas_run_db.xml](matlas_run_db.xml), and the demonstration database
([examples/build_example_db.py](examples/build_example_db.py) /
`examples/matlas_example.sqlite`, queried read-only). Every finding below was
verified against a concrete record, query, or quoted contradiction before being
recorded.

**Verdict.** The schema core held: no seat could break the occurrence-key
discipline, the composite activity FK, the INTEGER validity join, folder==zip
parity, the referenced/presence split, or the raw-field round-trip. Nothing below
challenges decisions 12–15 themselves. The fire converged on three seams instead:
the two digests were never updated to follow decisions 14/15; the demonstration
database quietly deviates from the documents it demonstrates (and will be
pattern-matched by Phase 1); and the flat projection ignores statute R6. Numbering
continues the first review's audit trail.

## Blockers

### R27 — The digests do not cover what decisions 14/15 moved

The digest definition (run_db_schema.md) still enumerates "entity value/type/role,
both expressions' raws + referentials + roles, the three links". Decision 14 moved
every context party into `assertion_mention` and decision 15 moved the raw values
into component rows, so `assertion_mention` rows, `multiplicity`, `deleted` and the
component set are formally **outside** both `content_hash` and `claim_digest`.
Concrete failures: two ANPR records differing only in camera collide on
`claim_digest` (two independent detections presented as one duplicated fact); two
same-second messages to different recipients collide; a `multiplicity=137`
aggregate "duplicates" a single call; a deleted and a live pin converge, erasing
the source's own deletion claim from duplicate analysis.

The demo recipe then contradicts the doc twice: it hashes the whole `raw_json`
(format- and column-name-sensitive, so a re-disclosure in a different export
format never converges — the R5 capability silently absent while the docs say it
works), and it includes `output_ordinal`, which run_db_schema.md explicitly
excludes — reinstating the exact R5 failure (a preset fix that shifts one ordinal
breaks cross-run comparison).

**Change:** pin the canonical L1 projection normatively — ordered component rows
(part + raw_value; decide whether source_field is in), ordered mention
(mention_role, entity_type, value) triples, `multiplicity`, `deleted` **in**;
`linked_entity_id`/origins **out**; no `output_ordinal` in `claim_digest` — and
rewrite the demo's `digest()` to it. **Open decision** (the in/out list), then
docs-now + Phase 1 (ids).

### R28 — The stated UTC offset has no L1 home in the reference database

Verified: `temporal_component.part` in the demo contains only
`lower | upper | duration` — zero `stated_offset_*` rows, although the ANPR record
states `tz=+01:00` and worked example 1 shows it. Only the *applied* `1.0` survives
at L2, so "delete L2 and lose no evidence" is already violated in the artefact that
seeds Phase 1, and the R13 statute (stated offsets TEXT verbatim at L1) is not
honoured. Compounding: the `TemporalExpression` dataclass in run_db_schema.md still
declares `stated_utc_offset_lower: float | None`, contradicting both R13 (a stated
"CEST" is a designation, not a number) and decision 15 (offsets are component
parts, not dataclass fields).

**Change:** add the `stated_offset_*` component rows to the demo builder; reconcile
the family dataclasses with decisions 13/15 before `core/vocabularies.py` freezes
them. **Pin-down** (the rules exist; the artefacts disobey them).

### R29 — `resolved_entity` identity: three artefacts, three constraints

run_db_schema.md requires `UNIQUE(kind, value)` guarded to identifier kinds only (a
partial index). matlas_run_db.xml declares a **full** `UNIQUE(kind, value)` —
verified to reject a second `('person', 'John Smith')`, the exact behaviour R2
outlaws. The demo DDL has **no** unique at all plus `INSERT OR IGNORE` — verified
to accept two identical identifier nodes, readmitting §2d at the attribution layer,
and lacking the CHECK that bars individual kinds from the content-key path
(entity_model.md). Phase 1 derives DDL from one declaration; three incompatible
sources block it.

**Change:** one declaration — partial UNIQUE + kind-class CHECK per
entity_model.md — propagated to the XML and the demo. **Pin-down.**

## Major

### R30 — The flat projection re-commits the founding defect

The database distinguishes testimony from measurement; the demo flat view does not.
Its column list omits the three link vocabularies, `spatial_referential`, the
integration `method`s, the beam sector, `exhibit_ref`/examiner, `multiplicity`, and
any duplicate marker. Verified consequences: the witness's "près de la gare", the
geocoded police register and a GPS fix all print bare coordinates with
`locates=self`; a 65°-wide tower sector prints as a point; the presence count
(Q3-style) contains the re-disclosed call and the double-ingested fix twice with no
flag, inflating corroboration; twenty recipients collapse into one comma-joined,
comma-ambiguous cell; the computed CDR upper bound (start + duration) is
indistinguishable from a stated one. All of this is the column list, not the
schema — statute R6 already mandates the columns; the demo must not anchor Phase 5
as it stands.

**Change:** the Phase-5 flat list honours R6 in full (three links, referential,
methods, locates, beam columns, duplicate-count/flag from `claim_digest`,
multiplicity, exhibit_ref, examiner) plus a per-mention long-form export (or a
recipient count) for context mentions. **Pin-down**, except one **open decision**:
whether `Locates` needs a distinct label for claimed-self (`CLAIMED_AT` currently
derives to `self`), or whether the projected `entity_position_link` column is
disambiguation enough.

### R31 — The demo silently introduced plural integrations

The demo DDL uses `UNIQUE(expression_id, method)` plus a `preferred` flag (Q8 shows
two cell-register versions coexisting) where run_db_schema.md says v1 is
single-integration (`UNIQUE(expression_id)`). No decision-log entry records the
change, and it is half-built: `spatial_integration` has the partial unique guard
(`WHERE preferred = 1`); `timespan_integration` does not — two `preferred=1` rows
were verified to be accepted — and the flat query joins without a `preferred`
predicate, so the day a second temporal integration lands, one line per assertion
breaks by row fan-out.

**Change:** either the doc adopts the pattern (decision-log entry; both partial
indexes; `preferred = 1` predicate in every projection join) or the demo reverts to
the v1 rule. **Open decision.**

### R32 — Vocabulary consolidation is a Phase-1 gate

The families.py proposal in run_db_schema.md has no `MentionRole` enum at all
(decision 14's own vocabulary), and lacks `camera`, `case_ref`, `cell`, `bssid`,
`terminal`, `organization` (EntityType), `victim` (EntityRole), `asset_ref`
(SpatialReferential), `incident`, `navigation_search` (ActivityKind), `alias_of`,
`registered_to` (RelationType — prose has them; the demo *stores* `registered_to`).
A CHECK derivation from the proposal as written rejects the demonstration
database's own data. Additionally `victim` currently exists in both EntityRole and
MentionRole with no precedence rule, and the prose says `unix_epoch` where the enum
says `unix_epoch_s/_ms/_us`.

**Change:** consolidate every ⊕ addition from model_worked_examples.md and
entity_model.md into the single declaration before `core/vocabularies.py` is
written; state the victim rule (subject-role when placed, mention-role when
context). **Pin-down.**

### R33 — Decision-14 boundary cases needing a stated rule

Verified or constructed cases with no doc coverage: (a) the two-leg CDR lists the
callee twice in `counterpart` — once as leg A's mention, once as leg B's subject
via the activity — and determinism rule 6 orders aggregations but does not
deduplicate; nor is it stated whether the counterpart mention persists when the
B-leg earns its own assertion; (b) the self-reporting victim ("I, M. Keller, was
robbed…") is simultaneously the placed subject and the reporter of her own
statement; (c) a hotel booking naming guest + booker + payer against one address:
three parties, one placement; (d) an EXIF person tag: the tag rides a placed
record, but named ≠ placed; (e) aggregates: `multiplicity=137` is one line and 137
placements, and `multiplicity` is populated nowhere in the demo.

**Change:** one worked paragraph per case in model_worked_examples.md, plus the
projection dedup rule. **Pin-down** (b–d may each need an owner ruling on which
party is placed).

### R34 — Missing natural UNIQUEs, keys, and indexes

`entity_relation` has no UNIQUE on its declared natural key
(subject ⟂ object ⟂ relation_type ⟂ valid_from) in doc, XML or demo — re-running
the R4 ingest path over the same contacts record inserts a silent duplicate edge,
and "fail loud" cannot fire because nothing declares the key. The XML assertion
lacks `output_ordinal`, `UNIQUE(raw_record_id, output_ordinal)` and
`UNIQUE(entity_id)` — keys, not skeletal columns; the §2a guard is absent from the
diagram. The declared index list (R21) needs `entity_relation(subject_id)`,
`entity_relation(object_id)`, `activity_link(object_activity_id)` and
`activity(raw_record_id)` — the demo's Q6/Q9 plans are full scans today.

**Change:** add the constraints and indexes to run_db_schema.md and the XML.
**Pin-down.**

### R35 — Convergence queries must union mentions

The demo *does* integrate attached-mention entities (verified: 14 of 15 mention
entities carry an `entity_integration`; the reporter correctly refuses as
person_name) — but no document states that mention entities are canonicalised like
any mention, and the reference query recipes (identifier timeline, tower
archaeology) scan subjects only, so "every appearance of +4179…" silently omits its
appearances as a counterpart or facet.

**Change:** state the rule (attached mentions receive integrations under the same
scoped-canonicalizability table) and define the canonical timeline query as
subjects ∪ mentions. **Pin-down.**

### R36 — The R3 sizing envelope is stale after decision 15

Component and mention rows put a blended ~19–20 rows per raw record; at 10–20 M
records that is roughly 53–106 GB of temp store (TEXT uuid keys and their
autoindexes included), beyond R3's 35–80 GB estimate, and `VACUUM INTO` needs free
scratch of the final size, so peak disk is ~2×. The mechanism (temp on-disk +
VACUUM INTO) survives; the stated numbers do not.

**Change:** restate the envelope and its assumptions in run_db_schema.md; no
schema change. **Pin-down.**

## Minor

- **R37 — Demo do-not-copy list.** `INSERT OR IGNORE` on resolved_entity (banned
  everywhere); Q7's `SELECT DISTINCT` — verified to hide the second disclosure of
  the same tower hit, a silent merge at the presentation layer; the digest recipe
  (R27); floats parsed before `raw_json` is stored (`46.2030` → `46.203`, violating
  canonical-JSON rule 5 — capture source text or `parse_float=str`). Mark the
  builder's header accordingly. **Pin-down.**
- **R38 — Doc drift sweep.** conceptual_model.md cites "decisions 6 and 11" (→ 6
  and 13) and "decisions 14 and 26" (26 is a finding, not a decision); "the line
  count equals the placement count" needs the positionless-claims clause;
  run_db_schema.md's flat-view section still says "one line per positioned
  participant" (pre-decision-14 phrasing); refactor_plan.md says "14 tables + meta"
  (the settled set is 19 + meta); matlas_run_db.xml's header changelog still
  describes the abolished assertion–entity junction; identity_model.md's "Where the
  portable digest lives" section predates `claim_digest` and still names
  `source_record`, and its opening still points at relational_model.md as the
  amended document. **Pin-down.**
- **R39 — The silent-loss inventory is blind to the new tables.** No row covers a
  dropped/duplicated `assertion_mention`, a dropped component (R28 is a live
  instance), `activity_link` or `mention_resolution`. Extend it per the first
  review's own closing instruction. **Pin-down.**
- **R40 — Component expressiveness limits.** Bearing + distance, what3words, and
  relative-anchor times ("3 days before Easter") degrade to `unknown` +
  `composite` — verbatim survives and integration refuses, which is honest, but the
  fallback rule should be stated, because `composite` is otherwise the opacity
  decision 15 abolished. **Pin-down.**
- **R41 — "No corners by construction" is convention, not constraint.** Nothing
  stops a preset attaching a mention that points at another assertion's subject
  entity, or double-emitting a facet as a placed subject with a copied cell. A
  per-record disjointness lint/test (or CHECK) closes it. **Open decision**
  (lint vs constraint).
- **R42 — Defined-but-never-exercised paths.** No negated/alibi claim exists
  anywhere — a `NEVER_DURING_INTERVAL` assertion would currently flatten to a
  normal presence line and be counted by a presence query; and the attribution join
  is only demonstrated with a single open-ended holder — no closed `valid_to`, no
  overlapping-holders case, no rule when two relations cover the instant. Add both
  to the demo before relying on them. **Pin-down.**

## Confirmed strengths (second review)

Attacked and survived: occurrence keys under the two-roles-one-record stress
(distinct entity rows per occurrence, reunited only by canonical GROUP BY); the
corner-ownership UNIQUEs including `assertion(entity_id)`; the R7 composite
activity FK, integrated verbatim in doc, XML and demo; `GROUP BY content_hash`
still catches folder==zip (six groups in the demo); typed-NULL `device` mentions
never merge across the deliberate double-ingest; `locates` is derived and never
stored; the R20 write order; the referenced/presence split holds in exploitation
(the chat pin and route search never enter the "who was there" answer); the Q8
round-trip traces every projected value to a verbatim field or a named method; and
`mention_resolution` / `activity_link` acts are separately attributed.
