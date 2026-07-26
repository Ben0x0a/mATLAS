# Identity model — what carries an ID, and why

> **Note.** The RULE this document states — *identity is not equality; a primary key is an
> occurrence key; a content hash is a finding* — governs the settled schema. The **exception
> list and table inventory below are historical**: the schema they describe was superseded by
> [schema.md](schema.md). In the settled model the `entity` table holds *mentions*
> (occurrence-keyed, never deduplicated); the only sanctioned content keys are the *identifier
> nodes* of the deferred analyst layer (see [entity_model.md](entity_model.md)); person and
> organisation nodes are occurrence-keyed. The naming conventions and the resilience rules below
> remain normative.

It settles a defect found while implementing the run database: it used **content addresses as
primary keys**, so two byte-identical acquisitions collapsed into one row and the second
artefact's path was lost
from the record entirely.

This document states the rule, grounds it in established practice, and defines the schema
and the naming conventions. The viewable ER diagram is
[matlas_run_db.xml](matlas_run_db.xml) (open at <https://sql.toad.cz/>).

## The rule

> **Identity is not equality.**
> A *content hash* answers "are these the same bytes?".
> A *surrogate key* answers "which reading is this?".
> A primary key must be the second kind. Never the first.

Two exhibits with the same hash are still two exhibits. The tool records that their
hashes match; it never merges them.

### The defect this replaces

`row_uid` was a content address that deliberately excluded `input_file_path` and
`raw_source_path`, which gave it a genuinely valuable property: the same database read from
a loose folder or from inside a zip produces the same identifier, on any machine, in any
run. The implementation then made that content address a `PRIMARY KEY`.

Those two decisions are incompatible. A content address is *designed* to be equal for equal
content; a primary key is *required* to be distinct per row. When the same evidence appeared
twice, the store silently discarded the duplicate — assertion rows, and worse, the entire
`source` row recording that a second artefact had been opened.

The resolution is not to weaken the content address. It is to stop asking one field to do
two jobs.

## Two kinds of key

| Kind | Question it answers | Unique? | Portable? | Used as PK? |
| --- | --- | --- | --- | --- |
| **Occurrence key** (`*_id`) | which reading is this? | yes, by construction | no (scoped by provenance) | yes |
| **Content hash** (`*_hash`) | are these the same? | deliberately not | yes (same on any machine) | no — one documented exception |

The single exception is `entity`: the same MSISDN string genuinely *is* the same entity, so
`entity_id` is a content address and deduplication there is semantically correct. This is
the only place in the model where the tool is entitled to assert that two things are the
same thing.

Everywhere else, deduplication would be the tool inventing a claim about the world. That
was already the stated position of the design — the model design says *"the model never
presumes two sources observed 'the same' position"* — the ID scheme simply contradicted it.

## Why this is the standard answer

**Dimensional modelling (Kimball).** The first rule of key design is that a surrogate key,
not a natural or business key, is the primary key. Natural keys are owned by the source
system and change their meaning; surrogate keys insulate the warehouse. `row_uid` was a
natural key (derived from the row's own content) doing a surrogate's job.

**Data Vault 2.0 (Linstedt).** The pattern maps almost one-for-one onto this model. A *hub*
holds a business key (our `entity`). *Links* and *satellites* carry the facts and their
descriptive data (our `assertion`, `position`, `timespan`). Critically, every row records
its **record source** and load metadata, and the model is **insert-only** — identical data
arriving from two feeds stays two rows, distinguishable by where it came from. Data Vault
exists because auditability requires exactly the property we lost.

**Content-addressable storage (Git).** Git is the cleanest illustration of the split. A blob
is addressed by the hash of its content, and two identical files share one blob — that is
correct deduplication of *content*. But the *tree entry* records each path the file appears
at, so Git never forgets that a file exists in two places. Our bug was deduplicating the
tree entry, not just the blob. `content_hash` is our blob id; `assertion_id` is our tree
entry.

**W3C PROV-O.** The traceability sidecar already speaks PROV. `run` is a PROV `Activity`,
`source` and `assertion` are PROV `Entity`s, and each assertion `wasGeneratedBy` the run
`wasDerivedFrom` a record. PROV entities have identity independent of their value precisely
so that two entities with equal values remain distinct and separately attributable.

**RDF reification / RDF-star.** An assertion here is a reified ternary fact — *this entity,
at this place, at this time*. Reification exists so a statement can itself be the subject of
statements (who asserted it, from what source, with what confidence). A reified statement
needs its own identity, distinct from its content, or you cannot attribute two independent
observations of the same fact to their respective sources.

**Digital-forensic practice.** ISO/IEC 27037 treats each acquisition as a distinct item of
potential evidence: two exhibits are not merged because their hashes match — the matching
hash is *recorded as a finding*. CASE/UCO likewise separates observable objects from the
provenance records that describe how each was obtained. A tool that silently collapses two
exhibits has destroyed chain-of-custody information.

Every one of these traditions arrives at the same place from a different direction: keep the
hash, keep it queryable, and never let it be the key.

## What each table's identity is

| Table | Key | Kind | Scoped by / derived from |
| --- | --- | --- | --- |
| `run` | `run_id` | occurrence | this execution (`started_at` + input path) |
| `source` | `source_id` | occurrence | **outer artefact** + inner path + subunit + preset |
| `source_record` | `source_record_id` | occurrence | `source_id` + `line_no` |
| `assertion` | `assertion_id` | occurrence | `source_record_id` + `output_ordinal` |
| `entity` | `entity_id` | **content** | `entity_label` + `entity_type` |
| `position` | `position_id` | occurrence | its own reading (owned via `assertion.position_id UNIQUE`) |
| `timespan` | `timespan_id` | occurrence | its own reading (owned via `assertion.timespan_id UNIQUE`) |

### The assertion is a pure link

`assertion` carries no measurements of its own. It takes one `entity`, one `position`, one
`timespan`, one `source_record`, and records only the *metadata of the statement*: the three
link vocabularies (`entity_position_link`, `entity_time_link`, `spatial_temporal_link`),
`attribution_origin`, `deleted`, and the untangle ranks. The three corners hang **off** the
assertion by foreign key — the symmetric triangle: entity, position, timespan, joined by the
fact that links them.

### Why the corners point the way they do, and why sharing is impossible

The FK lives on the assertion (`position_id`, `timespan_id`), not the reverse, and each is
declared **`UNIQUE`**. That single constraint is what enforces observation semantics in the
schema rather than in a comment: a position row can be owned by at most one assertion, so two
assertions with identical coordinates can never point at the same row — they get two rows.
The corner ids are therefore *occurrence surrogates*, not content addresses. `position` and
`timespan` each carry their own measurement provenance (`latitude_source_field`,
`lower_source_field`, `raw_position`, …), which is the deeper reason two readings are never
the same row even when their numbers match.

Two further consequences worth stating plainly.

`source_id` now includes the outermost artefact opened. Two zips holding a byte-identical
database therefore produce **two** source rows, both recorded, both with their own
`input_file_path` and `container_chain`. They share a `fingerprint_sha256`, which is how an
examiner discovers they are the same evidence.

`source_record` is the former `record` table, renamed: it is the *source's* record, and its
`native_record_uid` column holds the natural key the artefact itself assigned the row
(projected to the CSV as `source_record_uid`), distinct from our surrogate
`source_record_id`.

## Where the portable digest lives

`assertion.content_hash` is the path-independent content address — today's `row_uid`
derivation, unchanged, demoted from key to column and indexed. It buys back every property
the old scheme had, without the collision:

```sql
-- folder == zip parity: the same evidence yields the same content_hash
SELECT content_hash FROM assertion WHERE assertion_id = ?;

-- duplicate evidence becomes a finding, not a silent drop
SELECT content_hash, COUNT(*) AS copies, GROUP_CONCAT(DISTINCT s.input_file_path)
FROM assertion a
JOIN source_record r ON r.source_record_id = a.source_record_id
JOIN source s ON s.source_id = r.source_id
GROUP BY content_hash HAVING copies > 1;

-- the same source file acquired twice
SELECT fingerprint_sha256, COUNT(*) FROM source
GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;
```

Duplicate evidence stops being an accident of the key space and becomes a query an examiner
can run and report.

## Naming conventions

Following ISO/IEC 11179 (names must be unambiguous without external context), with the
repo's existing unit-suffix habit:

| Suffix | Meaning | Example |
| --- | --- | --- |
| `_id` | opaque surrogate occurrence key (uuid5) | `assertion_id` |
| `_hash` | content address; equal ⇒ equal content | `content_hash` |
| `_sha256` | a named hash algorithm over file bytes | `fingerprint_sha256` |
| `_at` | ISO-8601 UTC instant | `started_at` |
| `_count` | a cardinality | `record_count` |
| `_us` / `_m` / `_kmh` / `_deg` | the unit the number is in | `time_accuracy_us` |
| `_source_field` | which source column a value came from | `latitude_source_field` |

A reader who sees `entity_id` knows it is opaque and joins somewhere. A reader who sees
`content_hash` knows it may legitimately repeat. That is the whole point of the convention.

## Resilience

The schema is designed to fail loudly rather than lose data.

- **No `INSERT OR IGNORE`**, except on `entity`, where dedup is the intended semantics. A
  duplicate insert anywhere else is a bug and must raise, not silently vanish. This is the
  single change that would have surfaced the original defect immediately.
- **`UNIQUE` constraints on the natural keys** — `source(run_id, input_file_path,
  raw_source_path, subunit, preset_id)`, `source_record(source_id, line_no)`,
  `assertion(source_record_id, output_ordinal)`, and `assertion(position_id)` /
  `assertion(timespan_id)` (the corner-ownership guard). The surrogate key is unique by
  construction; the natural key is unique by constraint. If the derivation ever drifts, the
  database says so.
- **`PRAGMA foreign_keys = ON`.** SQLite does not enforce foreign keys by default. An
  orphaned corner row is a corrupt record.
- **`CHECK` constraints from the controlled vocabularies** in `model_atlas.core.vocabularies`,
  allowing `NULL` wherever the model allows it today.
- **Indices** on `assertion.content_hash` and `source.fingerprint_sha256`, so the duplicate
  queries above are cheap on a large run.

## Consequences for the flat output

`row_uid` in the CSV becomes the occurrence key (`assertion.assertion_id`) — unique per
output row, which is what the name always promised. A new `content_uid` column carries
`assertion.content_hash`, the portable digest.

This **intentionally changes `row_uid` values and adds a column**, so the golden CSVs are
regenerated once, deliberately, and reviewed. Byte-parity with the pre-refactor output is
retired as an oracle at this point; the parity that remains meaningful is that the same
evidence read from a folder or a zip yields the same `content_uid`, which the folder==zip
test now asserts directly.

`utc_offset_hours` and `time_accuracy_us` remain `TEXT`: the first legitimately holds a
float, `NULL`, or the `"N/A"` sentinel (`UTC_OFFSET_UNKNOWN`); the second is an `int | float`
union. Both are re-coerced to a numeric dtype in the frame builder so CSV rendering is
unchanged.
