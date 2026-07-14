# Refactor plan — rebuilding mATLAS around the relational core

Status: **plan, 2026-07-11; superseded by the executable roadmap 2026-07-14.** The
brick architecture and phase structure below stand; the concrete, agent-ready task
breakdown now lives in [../tasks/roadmap.md](../tasks/roadmap.md) (T00–T90) with one
file per task. Phase 0 (docs closure) is **done**; the schema settled at **19 tables +
meta** (decisions 16–34, [run_db_schema.md](run_db_schema.md)). Rework of the whole
codebase around the settled model
([conceptual_model.md](conceptual_model.md) → [run_db_schema.md](run_db_schema.md) →
[matlas_run_db.xml](matlas_run_db.xml)), absorbing every statuted review finding
([model_review_findings.md](model_review_findings.md), Statute section). Owner's
architectural brief: **bricks around a core** — a small, pure core that knows the
model, and replaceable bricks around it so adding an input format or an output format
is one new file, never a change to the core.

## 1. Principles (the rules every phase obeys)

1. **The dependency arrow points inward, always.** Core imports nothing from bricks;
   bricks import core only; the app layer wires bricks together; shells (CLI, GUI)
   call the app layer only. A brick never imports a sibling brick.
2. **The store is the only meeting point.** Ingest bricks write layers 0–1,
   integration bricks write layer 2, analysis bricks write layer 3, export bricks
   *read*. Two bricks never talk to each other directly — they meet in the database.
3. **One declaration.** `core/vocabularies.py` (families.py's successor) declares
   every enum, family dataclass, and column; DDL, CHECKs, casts, units, and the flat
   column list all derive from it. Adding a model column stays a one-place change.
4. **Contracts are Protocols.** Reader, Method, Exporter are typed `Protocol`s with
   registries. A new brick = one module implementing the protocol + one register
   call. No core edit, no pipeline edit.
5. **Fail loud, never merge.** No `INSERT OR IGNORE` anywhere in ingest; UNIQUE
   violations raise; the silent-loss inventory in run_db_schema.md is the test list.
6. **Evidence / derivation / analysis never blur.** L0–L1 writes happen only during
   ingest and are immutable afterwards; L2 rows always carry a method; L3 is
   re-derivable and deletable without evidence loss.

## 2. Target layout (the bricks)

```
model_atlas/
  core/                     # THE CORE — pure, stdlib(+sqlite3) only, no I/O policy
    vocabularies.py         #   enums + derivations (Genesis, Locates) + family dataclasses
    ids.py                  #   uuid5 namespaces, \x1f join, content_hash + claim_digest
    schema.py               #   DDL derivation (19 tables + meta), CHECKs, indexes, user_version
    store.py                #   RunStore: temp on-disk SQLite, VACUUM INTO persist,
    #                           typed insert API per layer (write_source, write_assertion, …)
    projection.py           #   flat query generation (one line per assertion), orig_ explosion
    determinism.py          #   normative enumeration/canonicalisation rules (R9) as functions
  ingest/                   # INPUT BRICKS (L0 + L1 writers)
    discover.py             #   walk inputs, containers, self-ingestion guard (R16)
    containers.py           #   chain-inclusive raw_source_path (R10)
    fingerprint.py          #   main + sibling fingerprints (R17)
    readers/                #   ONE BRICK PER FORMAT, Reader protocol:
      csv_reader.py         #     iter_records(selector) -> RecordStream (ordered per R9)
      excel_reader.py
      sqlite_reader.py
      ufdr_reader.py        #     (future — lands here, nothing else changes)
    presets/                #   matcher, spec, loader, fields (referential/roles/activity fan-out)
    assemble.py             #   preset mapping × records -> activities + assertions + corners
  integrate/                # L2 BRICKS — Method protocol: (expression) -> integration + method str
    temporal.py             #   epoch decodes, tz/DST resolution, assumed-UTC
    spatial.py              #   passthrough_wgs84, unit conversions; geocode/cell-register later
    entity.py               #   canonicalizers per EntityType (E.164, lowercase email, …)
    registry.py             #   method registration + naming convention (R14)
  analyze/                  # L3 BRICKS
    untangle.py             #   ranking -> untangle_finding rows
    relations.py            #   operator/evidence-borne entity_relation writes (R4)
    activity_links.py       #   cross-record same-event linking (R8; defined, later)
  export/                   # OUTPUT BRICKS — Exporter protocol: (store, ctx) -> files
    registry.py             #   unchanged mechanism (it is already right)
    csv_flat.py             #   the flat CSV (a query + writer; split mode = partition)
    sqlite_db.py            #   persist the run DB (VACUUM INTO)
    kml.py, geoviz.py, …    #   each a query + serialiser
  app/                      # ORCHESTRATION (the only module that sees everything)
    pipeline.py             #   run(): discover -> read -> assemble -> integrate -> analyze -> export
    config.py, reporting.py, diagnostics.py, logging_setup.py
launcher/                   # CLI shell (argparse -> app.pipeline)
gui/                        # GUI shell (calls app.pipeline; worker thread as today)
```

Old → new mapping: `model/families.py` → `core/vocabularies.py` · `model/schema.py`,
`model/store.py` → `core/` (rewritten) · `sources/*` → `ingest/*` ·
`transforms/assemble.py` → `ingest/assemble.py` + `integrate/*` (the time-decode half
becomes temporal methods) · `transforms/rank.py` → `analyze/untangle.py` ·
`exporters/*` → `export/*` · `pipeline.py` → `app/pipeline.py`. `transforms/expr.py`,
`value_maps.py`, preset machinery move intact under `ingest/presets/`.

## 3. The four contracts

```python
class Reader(Protocol):          # ingest brick
    format: str                  # "csv" | "excel" | "sqlite" | "ufdr" | ...
    def subunits(self, source) -> list[str]: ...
    def iter_records(self, source, subunit) -> Iterator[RawRecord]   # R9 order, verbatim dicts

class Method(Protocol):          # integrate brick
    name: str; version: str      # + reference data -> the stored method string
    def applies_to(self, expression) -> bool
    def integrate(self, expression) -> Integration | None

class Analyzer(Protocol):        # analyze brick
    def run(self, store: RunStore) -> None      # writes L3 only (enforced by store API)

class Exporter(Protocol):        # export brick (today's registry, kept)
    def export(self, store: RunStore, ctx: ExportContext) -> list[Path]
```

The store's write API is layer-typed (`write_assertion` cannot touch L2/L3 tables),
so principle 6 is enforced by the only object bricks share.

## 4. Phases — each shippable, each gated by tests

**Phase 0 — close the paperwork (no code).**
Write `entity_model.md` (scoped canonicalizability, two node classes, mention_resolution,
typed validity bounds — statutes R1/R2); fold every *docs-now* statute into
run_db_schema.md (R5 claim_digest, R7 composite FK, R10 chain-inclusive path, R11 L1
deleted, R13 TEXT stated offsets, R20 write order, R22 drop, R24 entity_origin, R26
line rule) and the determinism section (R9); align conceptual_model.md (R8/R26
wording); update matlas_run_db.xml; fix identity_model.md's stale entity exception.
*Gate: docs agree with each other; the XML validates.*

**Phase 1 — the core brick, standalone.**
`core/` built and tested with no pipeline wiring: vocabularies (proposal in
run_db_schema.md + statuted additions), ids (both digests, canonical JSON), schema
(19 tables + meta + indexes), store (temp on-disk, VACUUM INTO, layer-typed API),
projection, determinism helpers. *Gate: unit tests — the silent-loss inventory as
constraint tests (each §2/R failure mode provably raises), double-run diffability
test, folder==zip digest test, schema-from-vocabularies snapshot.*

**Phase 2 — ingest on the new core.**
Readers keep their parsing logic, gain the Reader protocol + R9 enumeration order;
discover gains R16 guard, R17 sibling fingerprints, R10 chain paths; presets gain the
new fields (entity_type, referentials, roles, activity fan-out, linked_entity/entity
origins — R24); assemble rewritten to emit activities + assertions + corners into the
store. The old flat path still runs in parallel (both engines callable). *Gate: the
§2 scenario suite (dup zips, two devices, CDR legs) against the new store; existing
reader tests green.*

**Phase 3 — integrate layer.**
Temporal methods carved out of today's `_decode_time`/timezone transforms (they
already exist — they become named, versioned Methods); spatial passthrough + unit
conversion; entity canonicalizers with scoped canonicalizability. *Gate: every
integration row has a method; stated-vs-applied offset tests; DST interval tests
reuse the existing dst/timezone test corpus.*

**Phase 4 — analysis layer.**
Untangle reads the store, writes `untangle_finding` (rank semantics unchanged, now
versioned); relation ingest path (R4) + operator attribution; `activity_link` and
`mention_resolution` tables exist, unpopulated. *Gate: untangle parity test against
the old ranking on identical input.*

**Phase 5 — outputs become projections; the switch.**
`csv_flat` = projection query (new column list: content_uid, claim_digest finding
columns, entity_role, counterpart, activity_id, two offset columns, referentials,
locates, methods — R6); split mode = partition; sqlite export = VACUUM INTO; other
exporters ported one by one (each is small). Then the pipeline switches to the new
engine, the old flat path is deleted, and **goldens are regenerated once,
deliberately, and reviewed** — the accepted oracle change. *Gate: golden suite green
on regenerated goldens; GUI/CLI end-to-end run; export parity spot-checks (KML/geoviz
row counts and coordinates).*

**Phase 6 — sweep.**
Delete `model/`, `transforms/` remnants and the old pipeline paths; move tests to
mirror the brick layout; update architecture.md, testing_strategy.md,
source_adapters.md, preset docs; extend the silent-loss inventory with one row per
closed R-finding (per the review's own instruction). *Gate: full suite, `.venv/bin/python
-m pytest`, plus a real end-to-end run on a reference acquisition.*

## 5. Test strategy (what "gated" means)

- **Constraint tests** (Phase 1): for every row of the silent-loss inventory, a test
  that attempts the forbidden write and asserts it raises.
- **Scenario suite** (Phase 2): executable versions of rethink §2a–2d and the CDR /
  group-message / journey walk-throughs.
- **Determinism**: run twice on the same fixture → byte-identical DB dumps (modulo
  run row); folder vs zip → equal claim_digests.
- **Goldens**: byte-parity is the oracle *within* phases 2–4 (old engine still
  authoritative); regenerated exactly once at Phase 5 with a reviewed diff.
- **Vocabulary discipline**: a test asserting every stored enum value round-trips
  through vocabularies.py (no stray strings in the store).

## 6. Risks and their controls

| risk | control |
| --- | --- |
| big-bang breakage | old and new engines coexist through Phases 2–4; switch is one commit in Phase 5 |
| preset ecosystem churn (new fields) | new preset fields optional with documented defaults (referential inferred from format, role=subject, activity per record); lint warns, never breaks |
| golden regeneration hides a regression | the Phase-5 diff is reviewed column-by-column against the cross-check table in run_db_schema.md |
| scope creep from populate-later items | activity_link / mention_resolution / geocoding land as empty tables + registry slots; any population is its own future task |
| GUI coupling | GUI already calls the pipeline behind a worker; the app-layer API (`process()`-equivalent signature) is kept stable through Phase 5 |
