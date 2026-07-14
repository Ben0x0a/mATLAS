# Relational core — identity & assertion rethink (working notes)

Status: **HISTORICAL (superseded 2026-07-11).** The durable conclusions now live in
[conceptual_model.md](conceptual_model.md) (the ontology),
[run_db_schema.md](run_db_schema.md) (the settled schema, id scheme, and decision log)
and [matlas_run_db.xml](matlas_run_db.xml) (the ER diagram). §2 (the proved defects)
remains the reference for what the new schema must make impossible. The §5 schema and
§8 open questions below are superseded.

Original status: **in discussion, no code written.** This is the reasoning trail behind the
proposed schema, so the decisions can be reviewed before anything is built. It sits
alongside two artefacts it produced:

- [identity_model.md](identity_model.md) — the settled rule and conventions.
- [matlas_run_db.xml](matlas_run_db.xml) — the proposed ER diagram (open at <https://sql.toad.cz/>).

It supersedes nothing yet. When the open questions below are answered, the durable
conclusions fold into `relational_model.md` and `identity_model.md`, and this file becomes
history.

---

## 1. How we got here

Phase C (task T08) moved the run's data into an in-memory SQLite database. While validating
it, a subagent changed a test from asserting 4 output rows to 2, and made assertion inserts
`INSERT OR IGNORE`. That is the thread we pulled.

The immediate symptom was benign-looking (a duplicate collapsed). Pulling it revealed a
design fault that predates Phase C, and a second, worse one underneath.

---

## 2. What we proved (not argued — ran)

All against the current Phase-C code. Probes kept in `scratchpad/collide.py` and
`scratchpad/entprobe.py`.

### 2a. `row_uid` was never unique

The old flat pipeline emitted **4 CSV rows carrying only 2 distinct `row_uid` values** when
the same database appeared in two zips. Confirmed by running the scenario against `HEAD` in a
throwaway git worktree. So "row_uid uniquely identifies an output row" was already false
before any of this refactor.

### 2b. When does an assertion actually collapse?

`row_uid` (and `source_record_uid`) derive from
`source_fingerprint | raw_source_path | line_no`. Five scenarios:

| # | file bytes | inner path | result |
|---|---|---|---|
| A | identical | identical | **collapses** to 1 row |
| B | differ | differ | 2 rows, distinct ids |
| C | (one db, two identical rows) | — | 2 rows, distinct ids |
| D | identical | differ | 2 rows, distinct ids |
| E | **differ** | identical | 2 rows, distinct ids |

Reading: the collapse fires **only** when the literally-same file is presented twice
(A). Two different databases holding an identical row do **not** collide (E) — the file
fingerprint keeps them apart. So the collapse can never merge genuinely different evidence;
merging would require a SHA-256 collision. But when it does fire, it silently discards a
recorded observation.

### 2c. The worse one: a whole `source` row vanished

In scenario A the pipeline **matched 2 sources** but the run database recorded **one
`source` row** — the second artefact (`b.zip`), its path and container chain, was gone from
the record entirely. That is chain-of-custody information destroyed, not a cosmetic dedup.

### 2d. Two different phones became one entity

Two distinct devices, two databases, both presets saying `entity: const(device)` →
**one `entity` row**, both assertions pointing at it. The tool asserted two phones are the
same phone. Also: the linked entity is inserted with `entity_type = NULL`.

---

## 3. The root cause

One identifier was doing three incompatible jobs:

1. **Portable** — same evidence read from a folder or a zip yields the same id (achieved by
   *excluding* paths from the hash). This is a genuinely valuable property.
2. **Unique** — it is a table's `PRIMARY KEY`.
3. **Complete** — nothing is dropped.

You can have any two. Content-addressing buys portability; making that a primary key spends
completeness. Nobody decided to spend it — it fell out of overloading one field.

### The rule that resolves it

> **Identity is not equality.** A content hash answers "are these the same bytes?"; a
> surrogate key answers "which reading is this?". A primary key must be the second kind.

Two exhibits with the same hash are still two exhibits. Record that their hashes match;
never merge them.

---

## 4. Why this is the standard answer (not a preference)

Five independent traditions land in the same place:

- **Kimball (dimensional modelling)** — the PK is a *surrogate*, never a natural/business
  key. `row_uid` was a natural key doing a surrogate's job.
- **Data Vault 2.0** — hubs hold business keys (our `entity`); links/satellites hold facts
  (our `assertion`/`position`/`timespan`). Every row records its *record source*; the model
  is *insert-only*. It exists precisely because audit needs the property we lost.
- **Git** — a blob is content-addressed and shared; the *tree entry* records every path, so
  Git never forgets a file exists twice. Our bug deduplicated the tree entry, not the blob.
  `content_hash` is the blob id; `assertion_id` is the tree entry.
- **W3C PROV / RDF reification** — a reified statement needs identity distinct from its
  content, or two independent observations of the same fact can't be separately attributed.
- **ISO/IEC 27037 & CASE/UCO (forensics)** — each acquisition is a distinct exhibit; a
  matching hash is *recorded as a finding*, not a reason to merge.

---

## 5. The proposed schema

Seven tables. `assertion` is a **pure link** — it takes one entity, one position, one
timespan, one source_record, and carries only the statement's metadata.

```
run ──< source ──< source_record ──< assertion >── entity   (entity_id, linked_entity_id)
                                          │
                                          ├──1:1 position    (assertion.position_id UNIQUE)
                                          └──1:1 timespan     (assertion.timespan_id UNIQUE)
```

### The three corners are NOT symmetric

The test that separates them: **does the row carry provenance of a measurement?**

- `position` carries `latitude_source_field`, `raw_position`, accuracy columns…
- `timespan` carries `lower_source_field`, `lower_raw`, `temporal_source`…
- `entity` carries `label` + `type`. Nothing else.

So position/timespan describe *how this record measured this value* — two readings are never
the same row even when the numbers match, because they came from different source columns.
Entity has no provenance of its own (its provenance is the assertion), so it is shareable.

Cardinality therefore differs: **many-to-one** to `entity`; **1:1 owned** for position and
timespan.

### Corner ownership is enforced, not documented

`assertion.position_id` and `assertion.timespan_id` are FKs **with a `UNIQUE` constraint**.
That constraint mechanically forbids two assertions from pointing at one corner row, so
"observations are never deduplicated" is enforced by SQLite rather than by a comment. Corner
ids are plain occurrence surrogates.

### What each id is

| table | key | kind | scoped by |
|---|---|---|---|
| run | run_id | occurrence | this execution |
| source | source_id | occurrence | **outer artefact** + inner path + subunit + preset |
| source_record | source_record_id | occurrence | source_id + line_no |
| assertion | assertion_id | occurrence | source_record_id + output_ordinal |
| entity | entity_id | **content** | entity_label + entity_type |
| position | position_id | occurrence | its own reading |
| timespan | timespan_id | occurrence | its own reading |

`content_hash` (today's portable digest) becomes an **indexed column on `assertion`**, not a
key. Folder==zip parity becomes a query (`WHERE content_hash = ?`); duplicate evidence
becomes a query (`GROUP BY content_hash HAVING COUNT(*) > 1`); nothing is dropped.

### Naming (ISO/IEC 11179 + repo habit)

`_id` = surrogate occurrence key · `_hash` = content address (may repeat) ·
`_sha256` = named file-byte hash · `_at` = ISO-8601 UTC · `_count` = cardinality ·
`_us`/`_m`/`_kmh`/`_deg` = unit · `_source_field` = which source column a value came from.

### Renames from T08's schema

- `record` → **`source_record`** (it is the *source's* record).
- its natural key → `native_record_uid` column (CSV `source_record_uid`); our surrogate is
  `source_record_id`.
- corners: `PK = assertion_id` → own `*_id` PK + `UNIQUE` FK on the assertion.

---

## 6. Resilience choices

- **No `INSERT OR IGNORE`**, except on `entity` (where dedup is intended). A duplicate
  anywhere else must *raise*, not vanish — the single guard that would have surfaced the
  original bug on day one.
- **`UNIQUE` on every natural key** — `source(run_id,input_file_path,raw_source_path,subunit,
  preset_id)`, `source_record(source_id,line_no)`, `assertion(source_record_id,output_ordinal)`,
  plus `assertion(position_id)` / `assertion(timespan_id)`.
- **`PRAGMA foreign_keys = ON`** — SQLite defaults it off; an orphan corner is corruption.
- **`CHECK`** from the `families` enums, NULL allowed where the model allows it today.
- **Indices** on `assertion.content_hash` and `source.fingerprint_sha256`.

---

## 7. Trade-offs on the table

### 7a. Byte-parity oracle is retired (cost, accepted in principle)

`row_uid` becomes unique and a `content_uid` column is added → **`row_uid` values change and
the CSV widens to 46 columns**. Every task from T05 on was validated against
byte-identical golden CSVs; that oracle dies here. It is replaced by a sharper one: the same
evidence read from a folder or a zip yields the same `content_uid`, asserted *directly* by
the folder==zip test. Goldens get regenerated once, deliberately, and reviewed. This must
happen before T09 (exporters), which builds on the schema.

### 7b. Duplicate evidence: surface vs silent (recommendation: surface)

Keep the content-addressed view of duplicates, but never let it delete a row. A duplicate
acquisition is two `source` rows + two assertion sets sharing a `content_hash`, discoverable
by query and reportable. "2 sources → 1 row with no explanation" is the state we reject.

---

## 8. OPEN QUESTIONS (need your call before code)

### Q1 — Entity scoping (the real defect from §2d)

`entity_id = uuid5(label|type)` treats *any* label as a global identifier. True for MSISDN /
IMEI / IMSI / email. **False** for a descriptive constant like `device` / `owner` → distinct
things merge.

- **Option A — namespaced type.** Global types (`imei`, `msisdn`, `email`, `case_subject`)
  dedup by `(type,label)`; descriptive types (`device_label`, `account_name`) are scoped by
  `source_id` so two `device`s stay distinct. Makes `entity_type` mandatory (also fixes the
  NULL-type linked entity). *Recommended.*
- **Option B — no ingest dedup.** `entity` becomes an observation like the corners; all
  merging happens in an explicit, reviewable resolution table. Most faithful to "identity is
  never inferred"; loses the hub convenience, one entity row per assertion.
- **Option C — status quo**, require presets to emit real identifiers. Rejected: silently
  wrong today, no warning to the preset author.

### Q2 — Entity roles

Two fixed columns (`entity_id` / `linked_entity_id`) don't generalise to caller/callee,
sender/recipient.

- **Keep two FK columns** — pragmatic, matches the flat CSV's fixed `entity`/`linked_entity`.
- **`assertion_entity(assertion_id, entity_id, role)` link table** — faithful, extensible;
  costs the flat columns (pivot the two known roles back out in the view). Choose only if
  multi-role facts (CDRs, messaging) are on the roadmap.

### Q3 — Resolution layer, now or later

`relational_model.md` already defers *place clustering* and *entity resolution*. The schema
leaves room for a later `position.place_id` / `entity.identity_id` FK to derived,
human-reviewed tables. Confirm it stays deferred (recommended) vs pulled forward.

### Q4 — `linked_entity` self-FK (raised, recommend NO)

A self-referencing `entity.linked_entity_id` would make attribution a permanent property of
the entity. It is a property of the *statement* (hence `attribution_origin`), and would fork
the content-addressed entity per run. Keep `linked_entity_id` on the assertion.

---

## 9. Sequencing once decided

1. Amend `relational_model.md` + finalise `identity_model.md` with the Q1/Q2 answers.
2. Rework `model_atlas/model/schema.py` (tables, `UNIQUE`/`CHECK`/FK-on, `assertion_flat`
   view + `content_uid`).
3. Rework `model_atlas/model/store.py` id derivation + inserts (drop `INSERT OR IGNORE`
   except entity).
4. Regenerate golden CSVs; convert folder==zip test to assert on `content_uid`; add the
   duplicate-evidence query test.
5. Only then T09 (exporters query the store).
```
