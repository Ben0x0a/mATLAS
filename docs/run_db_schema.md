# Run database schema — the settled shape

Status: **settled with the owner, 2026-07-11; simplified by the owner 2026-07-13
(decisions 16–20: entity refocus, analyst graph deferred).** This is the
implementation shape of [conceptual_model.md](conceptual_model.md); the editable ER
diagram is [matlas_run_db.xml](matlas_run_db.xml) (open at <https://sql.toad.cz/>);
the identity discipline is [identity_model.md](identity_model.md); the defects this
makes structurally impossible are
[relational_model_rethink.md](relational_model_rethink.md) §2 (now historical).
Design priority set by the owner: **fidelity to reality, not performance — and only
what is really needed.** The review record is
[model_review_findings.md](model_review_findings.md) (R1–R26 statuted; R27–R42
partly mooted by the 2026-07-13 simplification, see its supersession note).
[entity_model.md](entity_model.md) now describes the **deferred** analyst layer, not
the run schema.

SQLite, **temp on-disk** during the run (`journal_mode=OFF`, `synchronous=OFF` — the
run DB is reproducible scratch), persisted via `VACUUM INTO` (R3; this removes the
old backup-deadlock constraint). `PRAGMA foreign_keys = ON`. `families.py` stays the
single declaration of every vocabulary; `schema.py` derives DDL and CHECKs
one-directionally.

## The model in one paragraph

A **Raw Record** (verbatim, inside a **Source**, inside a **Run**) yields
**Assertions**. An assertion is one entity, in one role, within one **Activity**, at
one place expression, at one time expression — qualified by the link vocabularies.
The two corners follow one pattern: an **expression** stores the source's own words
(header + component rows); an **integration** stores the derived canonical form
(1:1, private, method-stamped). The **entity** is one lean row: a normalized
identifier, the normalization that produced it, what it is, and whom it concerns.
Sameness — of entities, places, times, or activities across sources — is a query
(GROUP BY the normalized identifier / canonical values) or an explicit analysis act,
never an ingest-time merge. Everything analyst-graph-shaped (person chains, validity
intervals, mention resolution, cross-record activity linking) is **deferred out of
v1** (decision 16) — designed in entity_model.md, absent from the run schema.

## Tables (by layer)

### Layer 0 — acquisition (verbatim, immutable, never merged)

| table | columns | keys / notes |
| --- | --- | --- |
| case_metadata | case_id, case_number, title, description, created_at | **The root (decision 28)** — uuid4 PK; exactly ONE row per run-DB file (one case per database). `case_number` is the user-facing identifier. Hierarchy: case → run → source → raw_record → assertion. Physical name `case_metadata` (owner ruling 2026-07-14: `CASE` is an SQL keyword; columns keep the `case_` prefix) |
| run | run_id, case_id FK NOT NULL, input_path, started_at, finished_at, tool_version, settings_json, examiner | PK run_id (**uuid4** — the only non-deterministic id; feeds no child id); `case_id` FK (decision 28); input_path = the operator's `--input` root (decision 24: run grain); **settings_json is normatively COMPLETE (decision 39)**: every operator argument and option, canonical JSON — the run is reproducible from its row; selected presets via `run_preset`; examiner optional (R23) |
| matlas_meta | schema_version, families_version (one row) + `PRAGMA user_version` + SQLite `application_id` | versioning (R19) and the self-ingestion stamp (R16): discovery refuses/flags stamped artefacts |
| archive | archive_id, path, cd_sha256 | **One row per container path (decision 38, revising 29)** — id from the **path only** (§2c: a content hash is never a key), UNIQUE(path), `cd_sha256` a plain INDEXED column. "Same archive bytes twice" (two identical-bytes exhibits, or across runs) is a **query/finding** (`GROUP BY cd_sha256`). `cd_sha256` **reuses `model_atlas/sqlite/integrity.py` (`fingerprint_zip` / `zip_resume_key`)** — no new hashing |
| run_archive | run_archive_id, run_id FK, archive_id FK, parsed_at, skipped | **The run↔archive link (decision 38)**: one run opens many archives, one archive may appear in many runs — UNIQUE(run_id, archive_id). EVERY encounter is recorded, including an **opt-in skip** (`skipped=1`, never traceless): re-walking may be skipped only when the operator opted in (recorded in settings_json) AND a previous run parsed the same `cd_sha256` with the same preset set (key + version + definition_digest) |
| run_preset | run_preset_id, run_id FK, preset_id FK | **The run↔preset link (decision 39)**: every preset SELECTED for the run (reusing the preset table), recorded even when a preset yields no assertion. UNIQUE(run_id, preset_id) |
| source | source_id, run_id FK, input_file_path, inner_path, data_source, format, subunit, fingerprint_sha256, sibling_fingerprints_json, exhibit_ref, source_tier, record_count | UNIQUE(run_id, input_file_path, inner_path, subunit); INDEX fingerprint_sha256 — the id derives from **paths, never the fingerprint** (§2c fix). **Preset-free (decision 27)**: a source is *what was opened* (folder→file, zip→inner file — D29); one file matching several presets is ONE source row and ONE set of raw_records — the reading lives on the assertion. `inner_path` = chain-inclusive container path (R10); `data_source` (decision 24, source grain) = where the data originally lived (UFDR/tool manifest device path), NULL for primary acquisitions. Fingerprint = main file bytes; siblings in sibling_fingerprints_json (R17); exhibit_ref (R23) |
| preset | preset_id, key, name, version, definition_digest | the companion table (decision 27): one row per preset applied in this run — uuid5(key ⟂ version), a config row whose definition IS its identity (same argument as the old method registry). UNIQUE(key, version). **definition_digest (decision 40)**: sha256 of the canonical preset definition — catches edits the author forgot to version-bump (same key+version, different digest across runs = a surfaced finding; skip eligibility compares digests, never just versions) |
| raw_record | raw_record_id, source_id FK, line_no, native_record_uid, input_record_id, recovery_state, raw_json | UNIQUE(source_id, line_no); raw_json is the COMPLETE source row |

#### The source hierarchy (V1 methodology, reinstated — decision 24; grain fixed by the owner)

The V1 provenance columns all survive — **each at its natural grain, duplicated
nowhere** (owner's ruling: the fields must exist, not be repeated per assertion):

| V1 CSV column | grain | home | projected from |
| --- | --- | --- | --- |
| (new) | run | run.input_path — the operator's `--input` root | run |
| input_file_path | source | source.input_file_path (outermost artefact opened) + inner_path, format, subunit | source |
| raw_source_path | source | source.data_source — where the data originally lived (UFDR/tool manifest device path); NULL for primary acquisitions | data_source, falling back to input_file_path!inner_path |
| source_tier | source | source.source_tier: primary (acquisition parsed) vs secondary (tool export parsed, interpretation inherited) | source |
| preset_id, preset_name | **assertion** (via the preset companion table — decision 27) | assertion.preset_id → preset | preset |
| input_record_id | record | raw_record.input_record_id (human locator) + native_record_uid | raw_record |
| source_record_uid | record | raw_record_id (surrogate) | raw_record |
| row_uid | assertion | assertion_id | assertion |
| source_label | **assertion** | assertion.source_label — the one provenance field the owner keeps per claim | assertion |

When a tool export names a *per-record* original path, that value stays verbatim in
raw_json and reaches the CSV via the orig_ passthrough — the lean choice; the
structured `data_source` is source-grain only.

### Layer 1 — assertion (the source's words)

| table | columns | keys / notes |
| --- | --- | --- |
| activity | activity_id, kind, context_json | one real-world happening. **context_json (decision 18)**: preset-mapped, semi-structured activity context — the callee, the caller's IMEI, the recipients list — canonical JSON, inside the L1 digest projection, projected to the flat counterpart/facets columns. **Bonus table (decision 35)**: it only groups the legs of one record; everything exploitable reads from entity/links/roles, and no mandatory CSV column sources from it |
| activity_record | activity_record_id, activity_id FK NOT NULL, raw_record_id FK NOT NULL | UNIQUE(activity_id, raw_record_id). **Decision 20 (revised by the owner's 2026-07-13 diagram)**: the activity↔record binding is a link table, not a column — ingest writes exactly one row per activity (its own record; the R7 mis-attachment guard becomes an ingest invariant + lint); an *analysis* act may later attach further records to the same activity ("both operators' records describe this call"), which replaces the deferred activity_link table with something simpler |
| assertion | assertion_id, raw_record_id FK NOT NULL, **preset_id FK NOT NULL (decision 27)**, entity_id FK NOT NULL, entity_origin (R24), activity_id FK NULL, spatial_expression_id FK NULL, temporal_expression_id FK NULL, entity_role, entity_position_link, entity_time_link, spatial_temporal_link, deleted (R11), multiplicity (R15: NULL = 1), source_label, content_hash, claim_digest (R5) | **UNIQUE(raw_record_id, preset_id, output_ordinal)** — two presets reading the same record produce distinct assertions; UNIQUE on each corner FK (owned corners) and on entity_id; both digests indexed, repeat legally, never keys. linked_entity dropped (decision 17) — entity.concerns; data_source/source_tier at source grain (decision 24); source_label = the one per-claim provenance field |
| entity | entity_id, identifier, raw_identifier, identifier_type, type, concerns, concerns_origin, concerns_uid | **One lean row (decisions 17, 21, 22)**: `identifier` = **normalized at ingest** (E.164, lowercase email, …; NULL for a purely typed mention); `raw_identifier` = **as written in the source, verbatim** (decision 21 — the entity corner's L1 residence back on the row itself, not only in raw_json); `identifier_type` names which normalization applied (msisdn \| imei \| email \| plate \| account \| other \| none); `type` = what the thing is (device \| person \| vehicle \| camera \| …); `concerns` = the attributed party fixed at parsing (absorbs linked_entity), `concerns_origin` (operator_param \| preset_mapped), `concerns_uid` = digest of the case-folded/stemmed concerns (decision 22 — "A. Moreau" / "moreau a." group together; derived, never a key). Occurrence row, no FK, no dedup; convergence = GROUP BY (identifier_type, identifier); attribution grouping = GROUP BY concerns_uid. Normalization + stemming rule-set versions recorded once per run in matlas_meta |
| spatial_expression | spatial_expression_id, referential, spatial_source | HEADER only (decision 15): referential = encoding/CRS (epsg:4326 \| geohash \| address \| place_name \| cell_ref \| asset_ref \| region); spatial_source = the mechanism (GNSS \| WiFi \| cell \| …, the V1 position_source, symmetric with temporal_source). The values live in component rows; **spatial_role moved to the component (decision 25)** |
| spatial_component | spatial_component_id, spatial_expression_id FK, ordinal, part, spatial_role, source_field, raw_value | UNIQUE(spatial_expression_id, ordinal). One row per source field, verbatim. **`part` uses the PRESET target-field names (decision 23)**: raw_position (composite) \| latitude_wgs84 \| longitude_wgs84 \| altitude_m \| horizontal_accuracy_m \| vertical_accuracy_m \| horizontal_speed_kmh \| vertical_speed_kmh \| horizontal_speed_accuracy_kmh \| vertical_speed_accuracy_kmh \| heading_deg \| heading_accuracy_deg \| beam_azimuth_deg \| beam_width_deg (+ zone/easting/northing for gridded CRS). Any part value is accepted; only standard names are export-projected. **spatial_role per component (decision 25)**: vicinity \| entry \| exit \| origin \| destination \| waypoint — a value-group can carry its own role. A 1-field geohash is 1 row; lat+lon columns are 2; DMS is 6 |
| temporal_expression | temporal_expression_id, kind, referential, temporal_source | HEADER only (decision 15); referential: unix_epoch_s \| apple_cfabsolute \| iso8601 \| excel_serial \| local_string \| …; temporal_source = clock mechanism. **temporal_role and the stated offsets moved to the component; granularity dropped (decision 25** — fuzziness is carried by the raw value and the integration method) |
| temporal_component | temporal_component_id, temporal_expression_id FK, ordinal, part, temporal_role, stated_offset, source_field, raw_value | UNIQUE(temporal_expression_id, ordinal). **`part` uses the PRESET field names (decision 23)**: lower \| upper \| duration (+ lower_date/lower_time-style splits when a bound spans columns). **Per-component (decision 25)**: `temporal_role` (start \| end \| booking \| check_in \| check_out \| last_seen — each bound carries its own role: a visit's lower is check_in, its upper check_out) and `stated_offset` (the offset the source stated for THIS bound, verbatim TEXT — R13 honoured; decision 9's dual offsets are now per-bound columns, not parts). A date column + time column stay two verbatim rows; the integration combines them (datetime_combine) |

**The four link vocabularies** live on the assertion and absorb epistemic status
(`REPORTED_FOR`, `CLAIMED_AT`, `NEVER_DURING_INTERVAL` are already members):
`entity_role` (caller, callee, sender, recipient, reporter, subject…) — the entity's
part in the activity; the three triangle links as today. `spatial_role` / `temporal_role`
(origin, destination, vicinity, entry… / start, end, booking, check_in…) sit on the
expressions — owned 1:1, so equivalent to assertion columns, and each corner stays
self-describing.

### Layer 2 — integration (derived, private, method-stamped)

The integration tables are **the modelled space+time layer (decision 30)**: every derived
spatial/temporal measurement column the flat CSV exposes lives here, so the CSV's space and
time block is a pure projection and integration re-runs (new geocoder / tz-db / S2 level)
without touching L1. The CSV's *other* columns are **not** duplicated here — links / roles /
deleted / multiplicity stay on `assertion`, identifier / type / concerns on `entity`,
paths / tier / preset / data_source on `source`, input_record_id on `raw_record`.

| table | columns | keys / notes |
| --- | --- | --- |
| spatial_integration | spatial_integration_id, spatial_expression_id FK, method, latitude_wgs84, longitude_wgs84, altitude_m, horizontal/vertical_accuracy_m, the 4 speed columns, heading_deg, heading_accuracy_deg, beam_azimuth_deg, beam_width_deg | UNIQUE(spatial_expression_id) in v1 (drop the UNIQUE to allow candidates later); the unit conversion IS the interpretation, raws survive in raw_json. This is the complete derived-spatial block (decision 30) |
| spatial_index | spatial_index_id, spatial_integration_id FK UNIQUE, s2_cell_id, s2_level | **S2 index (decision 31)**: one row per point (per spatial_integration); `s2_cell_id` = the signed-int64 **leaf** S2 cell (deliberately **non-unique** — many points share a cell), INDEXED. Hierarchy is implicit in the id (ancestors = bit-truncations; descendants = a contiguous `[range_min, range_max]`), so concomitance queries do `WHERE s2_cell_id BETWEEN ? AND ?` on the index — no parent/child table. Built with `s2sphere` (`CellId.from_lat_lng`); level-walking lives in the concomitance engine. Optional precomputed ancestor-level columns (e.g. `s2_cell_l12`) can be added later for SQL binning, no redesign |
| timespan_integration | timespan_integration_id, temporal_expression_id FK, method, lower_unix_utc_us, upper_unix_utc_us, lower_datetime_iso, upper_datetime_iso, lower_date, upper_date, lower_time, upper_time, utc_offset_hours_lower, utc_offset_hours_upper, time_accuracy_us | UNIQUE(temporal_expression_id); applied offsets per bound incl. the `N/A` assumed-UTC sentinel. **Convenience projections (decision 32)**: `*_unix_utc_us` = canonical UTC µs; `*_datetime_iso` = ONE offset-tagged **local** ISO string (`…+01:00`; when zone unknown render the assumed clock with a loud trailing ` N/A`; empty when there is no instant); `*_date` / `*_time` = the local date / time-of-day (µs). Derived from unix µs + applied offset. Empty = no value; ` N/A` = value, zone unknown (kept distinct) |

`entity_integration` is **gone (decision 17)**: the entity corner trades its L1/L2
split for one normalized `identifier` column — the only corner to do so, accepted to
refocus on what is needed. The per-mention method is replaced by the run-level
normalization rule-set version (matlas_meta); the verbatim string remains in raw_json.

`method` is a text field naming rule + version + reference data (e.g.
`tz_localize/2026a`, `cell_register/2025-03`, `passthrough_wgs84`). A pass-through is
a method too: *derived value ⇒ documented derivation*, no exceptions.

**No conversion without an L2 home (invariant).** Values are never transformed in
place. Anything the tool converts — units, epochs, offsets, formats, geometry — must
be a canonical column on an integration table, method-stamped; its raw form survives
in `raw_json` (and the L1 source fields where structured). Passthrough (`orig_`)
values exist *because* they are verbatim: the moment a passthrough value needs any
transformation, it is promoted to a canonical L1/L2 column pair, never converted
where it stands. L3 carries no measurements at all.

### Layer 3 — analysis (about evidence, never inside it)

| table | columns | keys / notes |
| --- | --- | --- |
| analysis | analysis_id, assertion_id FK, method, record_type, record_rank | UNIQUE(assertion_id, method): the generic, method-attributed verdict table (decision 19 — was untangle_finding). Re-running a changed ranker ADDS rows, never mutates; the CSV projects the current method's verdict |
| edit_log | edit_id, ordinal, table_name, row_id, column, old_value, new_value, edit_kind, edited_by, edited_at | **Manual-edit audit (decision 34)**, for the edit panel. `edit_kind` ∈ update \| new_row \| empty_row. **Evidence protection**: L0/L1 rows (case/run/source/raw_record/assertion/entity/expressions/components) are NEVER updated in place — only `new_row` (mint a new occurrence, keep the original) or `empty_row`; integration/analysis rows may `update` in place. Every action logs one row here; the edit API in `model_atlas` enforces the protection, not the UI. **Full rewind (decision 41)**: `ordinal` is a monotonic UNIQUE journal position; `update` logs per-column old→new, `new_row`/`empty_row` log the FULL row snapshot (canonical JSON in new_value) — applying inverses in reverse ordinal order restores any earlier state; a rewind appends inverse entries, never erases the journal |

**Deferred out of v1 (decision 16)**: `resolved_entity`, `entity_relation`,
`mention_resolution`, `activity_link` — the entire analyst graph (person chains with
validity, mention→node resolution, cross-record "same call"). The design survives in
[entity_model.md](entity_model.md) and returns as a separate analysis layer when
needed; the run schema carries nothing for it.

Duplicate-evidence findings are
**views**: `GROUP BY content_hash HAVING COUNT(*) > 1` (claims) and
`GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1` (sources).

## The id scheme

uuid5 inputs joined with `\x1f` (unit separator — a `|` in a path forged equal keys),
one namespace per table. `run_id` feeds no child id, so two runs over the same evidence
produce equal ids and are diffable; the DB file is the run scope (cross-run aggregation
keys by `(run_id, *_id)`).

| table | key kind | derived from |
| --- | --- | --- |
| case_metadata | occurrence | **uuid4** (one per DB, decision 28) |
| run | occurrence | **uuid4** |
| archive | occurrence | path (decision 38 — §2c-consistent, diffable across runs) |
| run_archive | occurrence | run_id ⟂ archive_id (run metadata, like the run row itself — the diffability rule exempts run-scoped links); UNIQUE(run_id, archive_id) |
| run_preset | occurrence | run_id ⟂ preset_id (run metadata); UNIQUE(run_id, preset_id) |
| source | occurrence | input_file_path ⟂ inner_path ⟂ subunit (preset removed — decision 27) |
| preset | **content** (config) | key ⟂ version — a preset's definition is its identity |
| raw_record | occurrence | source_id ⟂ line_no |
| assertion | occurrence | raw_record_id ⟂ preset key ⟂ output_ordinal (decision 27) |
| activity | occurrence | raw_record_id ⟂ activity_ordinal (per-record at ingest) |
| activity_record | occurrence | activity_id ⟂ raw_record_id; UNIQUE(activity_id, raw_record_id) — ingest writes exactly one per activity |
| entity / spatial_expression / temporal_expression | occurrence | assertion natural key ⟂ corner tag |
| spatial/temporal_component | occurrence | expression_id ⟂ ordinal; UNIQUE(expression_id, ordinal) |
| the two integrations | occurrence | expression_id ⟂ method (⟂ candidate_ordinal when plural arrives) |
| spatial_index | occurrence | spatial_integration_id (1:1; decision 31) |
| analysis | occurrence | assertion_id ⟂ method |
| edit_log | occurrence | uuid4 (an audit event; decision 34) |

The only content-keyed table is `preset` (config: key⟂version). Every other table is an
occurrence key (decision 16 removed the last identity content key, resolved_entity, with
the deferred graph).

**The normative L1 projection** (R27 resolved, updated for decisions 17/18) — the
exact field list both digests hash, in order:
entity (identifier ⟂ identifier_type ⟂ type ⟂ entity_role) ⟂ spatial expression
(referential ⟂ spatial_source ⟂ components ordered by ordinal, each as
part ⟂ spatial_role ⟂ raw_value) ⟂ temporal expression (kind ⟂ referential ⟂
components likewise, each as part ⟂ temporal_role ⟂ stated_offset ⟂ raw_value)
⟂ the three links ⟂ deleted ⟂ multiplicity ⟂ activity (kind ⟂ canonical
context_json). (data_source is source-grain — decision 24 — and therefore outside the
claim-level digests, like every other source attribute.)
**Excluded, normatively**: `concerns`, `concerns_uid` + both origin columns (operator
input and derived groupings never move an evidence digest), `raw_identifier`
(format-variant; the normalized identifier already represents the mention — a
re-disclosure with different formatting must converge), component `source_field`s
(likewise for renamed columns), all input paths, all `*_id`s and run_id, all
integrations (register/tzdb upgrades must not move the digest), all analysis rows.

**`assertion.content_hash` (CSV `content_uid`)** = fingerprint_sha256 ⟂ preset key ⟂
line_no ⟂ output_ordinal ⟂ the L1 projection — the digest **scoped to the preset's
reading of this file**, now literally (decision 27 put the preset in the prefix);
folder==zip parity holds: no paths. `claim_digest` stays preset-free: two presets
extracting the same claim from one record legitimately converge there.

**`assertion.claim_digest` (R5)** = the L1 projection **alone** (no fingerprint, no
line_no, no output_ordinal). Two overlapping disclosures — different CDR files
sharing 3 000 records — converge here and surface as a *possible same-evidence*
finding instead of being presented as corroboration. Both digests indexed; both
repeat legally; neither is ever a key. Both use the canonical JSON form defined below.

## Determinism — normative enumeration and canonicalisation (R9)

The ids and digests embed `line_no` and `output_ordinal`; diffability is a promise
only if enumeration is pinned. Normative rules, each with a test:

1. **SQLite**: records enumerate in `rowid` order (`WITHOUT ROWID` tables: primary-key
   order). Recovered records (WAL / journal / freelist / carved) enumerate **after**
   all live rows, in recovery-scan order, continuing the same `line_no` sequence;
   their `recovery_state` marks them.
2. **CSV**: physical line order; `line_no` = 1-based data-line number (header
   excluded), blank lines counted.
3. **Excel**: sheet row order; `line_no` = the sheet row number, so blank rows shift
   nothing.
4. **Paths** feeding `source_id`: absolute, NFC-normalised, `/` separators; container
   chains joined with `!` (chain-inclusive `inner_path`, R10/decision 24).
5. **Canonical JSON** (for `raw_json` digests and method parameters): UTF-8, sorted
   keys, no insignificant whitespace, no float re-formatting (values kept as read).
6. **Aggregations in projections** (counterpart, context_mentions, facets): ordered
   by mention ordinal, then entity value — never by rowid.
7. **BLOB policy** (R18, exercised when UFDR ships): a binary cell becomes
   `{"$blob": {"sha256": …, "bytes": n}}` in raw_json; the bytes themselves are not
   duplicated into the run DB.

## The flat view

One CSV line per assertion — and per decisions 14/18 an assertion exists only per
*placement*, so the line count is the placement count (a one-legged CDR is 1 line,
never 3; a 20-recipient message is 1 line). `row_uid` = assertion_id; `content_uid` =
content_hash; `entity` = entity.identifier (+ identifier_type, type columns);
`linked_entity` = entity.concerns (origin projected); `counterpart` / `facets` /
further context = projected from **activity.context_json** (decision 18) and, for
detector/infrastructure records, already visible in the spatial expression itself
(the camera id IS the cell_ref/asset_ref component) — **bonus columns (decision 35)**:
of the activity, only `assertion.activity_id` is a useful projection (it regroups the
lines referencing the same activity); no mandatory CSV column sources from the
activity table; everything else the record
carried reaches the CSV via the orig_ passthrough. Coordinates/UTC come from the
(single, v1) integrations, with referential, locates (derived) and the methods as
mandatory columns (R6). No invented positions: an assertion without a spatial
expression yields empty coordinates by construction.

## Ingestion write order

run → source → raw_record → activity (with its context_json) + activity_record link
→ **entity + expressions + components first, then the assertion that references them** (R20: FKs
check immediately) → integrations → *(after ingest)* analysis rows (untangle etc.).
**No `INSERT OR IGNORE` anywhere**; no content-keyed table exists in the run schema —
a UNIQUE violation anywhere is a derivation bug and raises.

## Silent-loss inventory (each §2-class failure → its structural guard)

| failure mode | guard |
| --- | --- |
| two exhibits collapse into one source (§2c) | source_id from paths, not fingerprint; natural UNIQUE; no OR IGNORE |
| duplicate evidence drops an assertion (§2a) | assertion keyed by record+ordinal; content_hash never a key |
| two assertions share a corner or an entity row | UNIQUE on each corner FK and on entity_id |
| two phones become one entity (§2d) | entity rows are occurrences; nothing dedups at ingest; 'device' is a TYPE, identifier NULL — nothing exists to merge |
| register/tzdb upgrade rewrites evidence | integrations keyed by expression+method; digest excludes integrations |
| operator input mutates the digest | concerns + both origin columns excluded from both digests, normatively |
| a context party silently vanishes | it is in raw_json verbatim always; activity.context_json is inside the L1 projection, so dropping it moves both digests |
| an activity attaches to the wrong record | activity_record link: UNIQUE(activity_id, raw_record_id) + the ingest invariant "one link row per activity, its own record" (R7 as invariant + lint; analysis-time additions are separately attributed) |
| a positionless claim vanishes from CSV | flat rule: one line per assertion, corners nullable |
| merging two run DBs collides ids | run_id excluded from child ids + DB-file-is-run-scope rule |
| `\|` in a path forges equal uuid5 inputs | `\x1f` join; PK collision raises as backstop |
| an opt-in archive skip hides evidence untraceably | the skip is recorded (run_archive.skipped=1) and eligible only when a previous run parsed the same cd_sha256 with an identical preset set — key, version AND definition_digest (decisions 38/40) |
| a preset edit without a version bump changes results silently | preset.definition_digest: same key+version with a different digest across runs is a surfaced finding (decision 40) |

## Decision log (the audit trail of this design)

| # | decision | rationale |
| --- | --- | --- |
| 1 | Identity is not equality: occurrence keys everywhere; content hash demoted to indexed column | §2 defects: content-address-as-PK silently destroyed two exhibits' distinctness (see identity_model.md) |
| 2 | Expression vs integration split for all three corners | "geocoded lat/long is a choice": the source's words are evidence; every normalisation is a documented derivation (council: GeoSPARQL Feature/Geometry, OWL-Time, PROV) |
| 3 | Entity = mention (value + type), no dedup at ingest; canonical form in a private 1:1 integration | identification is not conversion; 'device' is a TYPE, not an identifier; sameness = GROUP BY canonical — merge risk removed by construction, not by CHECK rules |
| 4 | Activity replaces the assertion↔entity junction | owner's insight: a call = 2 assertions sharing an activity, each leg with ITS OWN position/time — a junction party could not carry its own position |
| 5 | Roles complete the triptych as plain vocabulary columns | entity_role / spatial_role / temporal_role are symmetric to the three links (same nature as the link enums, not FK tables) |
| 6 | epistemic column removed | the nuance already lives in the links (REPORTED_FOR, CLAIMED_AT, REFERENCES, NEVER_DURING_INTERVAL); vocabularies to be completed rather than a parallel column |
| 7 | Conversion vs convergence: integration rows never shared | discrete values (identifiers, UTC µs) converge by exact equality; continuous ones (coordinates) need an explicit resolved-place layer later — the asymmetry is in the world, not the schema |
| 8 | Resolved Entity + Entity Relation = the analyst graph, soft-joined by canonical values | home of linked_entity (typed, dated, attributed relations: "this person had this number from/to"); ingest never writes it |
| 9 | Dual offsets, stated vs applied, per bound | an interval can span a DST change; what the source said (L1) and what the tool applied (L2) are different facts |
| 10 | run_id = uuid4, excluded from all child ids | two runs over the same evidence must be diffable; the DB file is the run scope |
| 11 | `locates` is derived from EntityPositionLink, never stored | same rule as Genesis: a derived view can never contradict the edge it derives from (at→self, within_range_of/at_fixed_detector→infrastructure, references→referenced) |
| 12 | exactly one linked entity per assertion, fixed at parsing | owner's ruling: linked_entity is the entity specified at parsing — a single FK + origin column; the relation graph stays independent (person chains, validity) and never produces the CSV column |
| 13 | confidence & epistemic weight live in the link vocabularies | owner decision on the 2026-07-11 review: no dedicated confidence columns; numeric source-stated confidences (OCR scores, …) stay verbatim in raw_json / orig_ passthrough; the vocabularies are completed as new source types require |
| 14 | a party earns its own assertion ONLY when the record gives it its own placement; everything else is an attached context mention (assertion_mention) | owner's exploitation ruling on the worked examples: ANPR/CCTV/witness/robbery/shared-pin = one line; a CDR is one line (two only when the B-leg has its own cell, never three — the IMEI is a *facet* of the caller, not a party); the camera/reporter/victim are context of the placement, not placements. Attached mentions have no corners by construction — the junction's original flaw cannot recur; the activity remains for genuinely multi-placement records |
| 15 | expressions = header + component rows (part, source_field, raw_value verbatim) | a source expresses one value in 1..N fields (geohash in one, lat/lon in two, DMS in six, date+time per bound, start+duration); fixed raw/source-field columns cannot fit that. Components also give accuracy/speed/heading their structured L1 residence (raw + field, rows only where the source has them) with the canonical number at L2 — and they remove the last undocumented L1 transformation (date⊕time merge now happens in the integration, method-stamped) |
| 16 | the analyst graph is deferred out of v1 | owner's 2026-07-13 refocus: resolved_entity, entity_relation, mention_resolution and activity_link leave the run schema entirely — all four were populate-later; the design survives in entity_model.md and returns as a separate analysis layer when needed |
| 17 | entity = one lean row: identifier (normalized at ingest) + identifier_type + type + concerns (+ origins) | owner's simplification: the entity corner trades its L1/L2 split for a working identifier — verbatim survives in raw_json, the normalization rule-set version is recorded once per run; convergence = GROUP BY (identifier_type, identifier). `concerns` absorbs decision 12's linked_entity (the attributed party, fixed at parsing); entity_integration and the per-mention method are gone |
| 18 | context parties live in raw_json + activity.context_json + the spatial expression itself | owner's ruling: assertion_mention is dropped. The detector IS the spatial expression (the camera id is its asset_ref component); activity-scoped context (callee, IMEI, recipients) is preset-mapped into activity.context_json (canonical JSON, inside the L1 projection); everything else stays verbatim in raw_json. Decision 14's line-count ruling stands: one line per placement, a CDR never 3 |
| 19 | untangle_finding → analysis | one generic, method-attributed verdict table for all tool judgments about assertions |
| 20 | ~~activity keeps raw_record_id + composite FK~~ **revised by decision 26** | the record binding is load-bearing (R7) but now lives in the activity_record link table |
| 21 | entity.raw_identifier: the identifier as written, on the row | owner's addition: the entity corner regains its verbatim L1 residence beside the normalized working form — no join to raw_json needed to see what the source wrote |
| 22 | entity.concerns_uid: digest of the case-folded/stemmed concerns | owner's addition: "A. Moreau" / "moreau a." / "A.MOREAU" group by one derived key without merging rows; derived, never a key, excluded from evidence digests; stemming rule-set versioned per run |
| 23 | component `part` names = the preset target-field names | owner's ruling: one standard vocabulary from preset mapping to component to export (raw_position, latitude_wgs84, horizontal_accuracy_m, time_lower, utc_offset_lower, duration…). Any value is accepted at ingest; only standard names are export-projected |
| 24 | the V1 source hierarchy reinstated, each field at its natural grain | owner's ruling (grain fixed same day): run.input_path = the operator's --input root; source.input_file_path + inner_path (renamed from raw_source_path) = what mATLAS opened; source.data_source = where the data originally lived (UFDR/tool manifest, source grain — NOT per assertion; per-record originals stay in raw_json/orig_); source.source_tier; raw_record.input_record_id; assertion.source_label = the one per-claim provenance field. R22 confirmed at source grain |
| 25 | roles and stated offsets move to the COMPONENT; expressions keep referential + mechanism | owner's 2026-07-13 diagram: each bound/value-group carries its own role (a visit = one interval whose lower is check_in and upper is check_out; origin/destination tag their own value groups) and its own stated offset (R13: verbatim TEXT per bound). spatial_source (the V1 position_source mechanism) returns on the spatial expression, symmetric with temporal_source; granularity dropped — fuzziness lives in the raw value + the integration method |
| 26 | activity↔record binding = the activity_record link table | owner's 2026-07-13 diagram, revising decision 20: ingest writes exactly one link per activity (its own record — R7 becomes an ingest invariant + lint instead of a composite FK); analysis may later attach further records to the same activity, replacing the deferred activity_link table with a simpler mechanism |
| 27 | the preset moves from source to assertion (companion `preset` table) | owner's ruling: one file can match several presets — the source is *what was opened* (one row, one set of raw_records, no duplication); the preset is a property of the *reading* that produced each assertion. assertion.preset_id FK; UNIQUE(raw_record_id, preset_id, output_ordinal); content_hash gains the preset key in its prefix; claim_digest stays preset-free so two presets extracting the same claim converge |
| 28 | `case` is the root, one case per database | owner's ruling: case → run → source → …; case_id (uuid4), case_number user-facing; run.case_id FK. Groups a run DB under an investigation |
| 29 | source = what was opened; ~~archive dedup on the central-directory hash~~ **archive grain revised by decision 38** | folder→file, zip→inner file (zip ≡ folder, already the code's model). The `archive` table reuses `model_atlas/sqlite/integrity.py` (`fingerprint_zip`/`zip_resume_key`) — no new hashing; its case grain and hash key were revised by decision 38 |
| 30 | integration tables = the modelled space+time CSV columns | explicit scope: every derived spatial/temporal value the CSV shows lives on the integrations (re-runnable without touching L1); the CSV's other columns keep their home tables and are not duplicated there |
| 31 | S2 spatial index (`spatial_index`) for concomitance | one row per point, non-unique signed-int64 **leaf** `s2_cell_id` + level, indexed; hierarchy implicit in the id (range scans), level-walking in the engine; `s2sphere` pure-Python (all-OS, no compiler); optional precomputed ancestor columns later |
| 32 | temporal integration convenience projections | per bound, alongside `unix_utc_us`: one offset-tagged local `datetime_iso` (` N/A` when zone unknown, empty when no instant) + local `date` + `time`; UTC recoverable from the tagged ISO |
| 33 | preset conditional mapping (`if … then … elif … else …`) | a mapping value may be a conditional chain in the existing field()/const()/param() vocabulary; no-match-no-else → the `N/A` sentinel (distinct from unmapped/None). expr.py `Cond` + FieldSpec.condition + `_resolve_field` eval; time bounds a noted follow-up |
| 34 | `edit_log` — manual-edit audit, evidence protected | the edit panel: L0/L1 evidence edited only via new-row (never in place); integration/analysis mutable in place; every change (update/new_row/empty_row) logged with old→new, who, when; enforced in the model_atlas edit API |
| 35 | activity is a bonus grouping table — nothing load-bearing lives only there | owner's 2026-07-14 ruling: `assertion.activity_id` links the legs of a single record (two-leg CDR, route origin+destination); everything exploitable reads from the structured columns — `spatial_role` tells origin from destination, each leg's entity tells who was at which antenna, and a leg's counterpart is the OTHER assertion sharing its activity_id. Structure unchanged (`kind`, `context_json` stay; context_json remains digest-covered — R27 — for parties with no leg of their own). The CSV keeps **`activity_id`** (an assertion column — the one useful projection: it regroups the lines referencing the same activity); activity_kind / counterpart / facets / context are optional bonus columns, and no mandatory CSV column may source from the activity table |
| 36 | EntityType gains EVENT and POI; an event from a structured list IS the entity | owner's ruling: events (a robbery in a police register) come from structured lists — the event reference is the entity (identifier_type=case_ref, identifier == concerns; no witness/victim reference needed on the row, they stay verbatim in raw_json → orig_); POIs may be ingested as placed entities (type=poi). ActivityKind stays lean — no incident/navigation_search members: event semantics live in the entity, search semantics in the `references` link |
| 37 | assertion verbalization: the vocabularies carry phrase fragments | owner requirement (2026-07-15): the product must construct every assertion as one deterministic sentence — *"Entity `<identifier>` (referring `<concerns>`) was `<entity_position_link phrase>` at `<place>` `<spatial_temporal_link phrase>` and `<entity_time_link phrase>` from `<lower>` to `<upper>`"* (or *"at `<instant>`"* for an instant). Each member of the link/role vocabularies declares its verbatim phrase fragment beside its value (one declaration, in `vocabularies.py`); a core verbalizer assembles the sentence from the projection — negation reads as negation, `references` never reads as presence, empty corners are said, never invented. Consumers (a bonus CSV column, the GUI panels) call the one function |
| 38 | run↔archive is a link table (`run_archive`); archive id from the path; skip is opt-in, preset-scoped, recorded | owner's ruling (2026-07-15), revising decision 29: one run opens many archives and one archive may appear in many runs — a case-grained row could not say which run opened what, left re-discoveries traceless, and `UNIQUE(case_id, cd_sha256)` was the last key a content hash participated in (against decision 1/§2c). Now: `archive(archive_id = uuid5(path), path UNIQUE, cd_sha256 INDEXED)` + `run_archive(run_id, archive_id, parsed_at, skipped; UNIQUE(run_id, archive_id))`. Re-walking may be skipped ONLY opt-in (an operator option, recorded in settings_json) AND when a previous run parsed the same `cd_sha256` with the same preset set (key + version + definition_digest — a different preset or an updated definition forbids the skip); a skip writes `skipped=1`, never traceless. Sameness remains a query (`GROUP BY cd_sha256`); resume of an interrupted run keeps the operational `zip_resume_key` cache |
| 39 | the run records its full configuration: `run_preset` + complete settings_json | owner's ruling: `run_preset(run_id, preset_id; UNIQUE)` links the run to every preset SELECTED for it (reusing the preset table, recorded even when a preset yields no assertion), and `run.settings_json` normatively carries EVERY operator argument and option (canonical JSON) — the run is reproducible from its row |
| 40 | `preset.definition_digest` beside the version | owner's ruling: sha256 of the canonical preset definition, recorded per preset row — catches definition changes the author forgot to version-bump (same key+version, different digest across runs = a surfaced finding; decision-38 skip eligibility compares digests, never just versions). UNIQUE(key, version) and the id derivation are unchanged |
| 41 | `edit_log` allows FULL REWIND | owner's ruling: the log is a complete, totally ordered journal — a monotonic UNIQUE `ordinal`, per-column old→new for `update`, the FULL row snapshot (canonical JSON in new_value) for `new_row`/`empty_row` — so applying inverses in reverse ordinal order restores any earlier state. The edit API exposes `rewind(to_ordinal)`; a rewind appends inverse entries (itself logged), the journal is never erased |
| 42 | preset template mapping (`format(...)`) | owner's ruling (2026-07-15): a mapping value may compose several source values with literal connecting text, f-string style — `format("{field(FirstName)} {field(LastName)} from {field(City)}")`. `format(...)` is one more reference kind, so it composes with decision-33 conditionals (`… then format(…)`) and pipes; every `{…}` placeholder is an existing call-form ref (field/header/filename/param/preset/const), text outside braces is verbatim. Missing/NA placeholder → empty text; EVERY placeholder missing → the `N/A` sentinel (consistent with decision 33) |

## Proposed families.py

The single declaration the DDL derives from. Existing enums (`Genesis`,
`EntityTimeLink`, `SpatialTemporalLink`, `RecordType`, `SourceTier`, `RecordState`,
`UTC_OFFSET_UNKNOWN`) are kept unchanged; `NEW` marks additions. `Locates` is
**derived** from `EntityPositionLink` (like `Genesis` from `EntityTimeLink`), never
stored.

```python
class EntityPositionLink(str, Enum):
    """E-S edge, unchanged members — but now also the source of `locates`."""
    AT = "at"; WITHIN_RANGE_OF = "within_range_of"
    AT_FIXED_DETECTOR = "at_fixed_detector"; REFERENCES = "references"
    CLAIMED_AT = "claimed_at"; INFERRED = "inferred"; UNKNOWN = "unknown"

    @property
    def locates(self) -> Locates:
        # Derived, never stored: a derived view can never contradict its edge.
        return _EPLINK_LOCATES[self]


class Locates(str, Enum):                                   # NEW (derived)
    """Whose position the spatial expression carries."""
    SELF = "self"                      # AT, CLAIMED_AT
    INFRASTRUCTURE = "infrastructure"  # WITHIN_RANGE_OF, AT_FIXED_DETECTOR
    REFERENCED = "referenced"          # REFERENCES
    UNKNOWN = "unknown"                # INFERRED, UNKNOWN


class IdentifierType(str, Enum):                            # NEW (decision 17)
    """Which normalization produced entity.identifier. NONE = purely typed mention
    ('the device'): identifier stays NULL, nothing can ever converge."""
    MSISDN = "msisdn"; IMEI = "imei"; IMSI = "imsi"; EMAIL = "email"
    PLATE = "plate"; ACCOUNT = "account"; MAC = "mac"; IP = "ip"
    CELL = "cell"; CAMERA = "camera"; CASE_REF = "case_ref"
    OTHER = "other"; NONE = "none"


class EntityType(str, Enum):                                # NEW
    """What the thing IS (never an identifier): the old §2d fix."""
    DEVICE = "device"; PERSON = "person"; VEHICLE = "vehicle"
    ACCOUNT = "account"; CAMERA = "camera"; ANTENNA = "antenna"
    CASE = "case"; LABEL = "label"; UNKNOWN = "unknown"
    EVENT = "event"; POI = "poi"        # decision 36: placed events + points of interest


class EntityRole(str, Enum):                                # NEW
    """The entity's part in the activity — 4th member of the link triptych."""
    SUBJECT = "subject"; CALLER = "caller"; CALLEE = "callee"
    SENDER = "sender"; RECIPIENT = "recipient"; REPORTER = "reporter"
    OWNER = "owner"; UNKNOWN = "unknown"


class SpatialReferential(str, Enum):                        # NEW
    """Encoding / reference system of the raw place value (NOT its role)."""
    EPSG_4326 = "epsg:4326"; GEOHASH = "geohash"; MGRS = "mgrs"
    ADDRESS = "address"; PLACE_NAME = "place_name"; CELL_REF = "cell_ref"
    REGION = "region"; UNKNOWN = "unknown"
    # extend with further EPSG codes as sources require them (one-place change)


class SpatialRole(str, Enum):                               # NEW
    """The place's part in the activity."""
    VICINITY = "vicinity"; ENTRY = "entry"; EXIT = "exit"
    ORIGIN = "origin"; DESTINATION = "destination"; WAYPOINT = "waypoint"
    UNKNOWN = "unknown"


class TemporalKind(str, Enum):                              # NEW
    INSTANT = "instant"; INTERVAL = "interval"
    OPEN_LOWER = "open_lower"; OPEN_UPPER = "open_upper"


class TemporalReferential(str, Enum):                       # NEW
    """Format / reference system of the raw time value."""
    UNIX_EPOCH_S = "unix_epoch_s"; UNIX_EPOCH_MS = "unix_epoch_ms"
    UNIX_EPOCH_US = "unix_epoch_us"; APPLE_CFABSOLUTE = "apple_cfabsolute"
    WEBKIT = "webkit"; FILETIME = "filetime"; EXCEL_SERIAL = "excel_serial"
    ISO8601 = "iso8601"; LOCAL_STRING = "local_string"; UNKNOWN = "unknown"


class TemporalRole(str, Enum):                              # NEW
    """The time's part in the activity."""
    START = "start"; END = "end"; BOOKING = "booking"
    CHECK_IN = "check_in"; CHECK_OUT = "check_out"; LAST_SEEN = "last_seen"
    UNKNOWN = "unknown"


class ActivityKind(str, Enum):                              # NEW
    CALL = "call"; MESSAGE = "message"; TRIP = "trip"
    TRANSACTION = "transaction"; STAY = "stay"; OBSERVATION = "observation"
    UNKNOWN = "unknown"


class Origin(str, Enum):                                    # NEW (decisions 12/17/24)
    PRESET_MAPPED = "preset_mapped"; OPERATOR_PARAM = "operator_param"

# RelationType / RelationOrigin: DEFERRED with the analyst graph (decision 16,
# entity_model.md). MentionRole: removed with assertion_mention (decision 18).


# --- family dataclasses (one per table; assembly emits them, the store writes them)

@dataclass(frozen=True)
class Entity:                   # table: entity (decision 17 — one lean row)
    identifier: str | None      # NORMALIZED at ingest; None = purely typed mention
    identifier_type: IdentifierType   # names the normalization applied
    type: EntityType            # what the thing is
    concerns: str | None        # the attributed party, fixed at parsing
    concerns_origin: Origin | None

@dataclass(frozen=True)
class Component:                # tables: spatial_component / temporal_component
    ordinal: int
    part: str                   # from the per-corner part vocabulary
    source_field: str | None
    raw_value: str | None       # VERBATIM

@dataclass(frozen=True)
class SpatialExpression:        # table: spatial_expression (HEADER, decision 15)
    referential: SpatialReferential  # encoding/CRS of the raw value
    spatial_role: SpatialRole | None # absorbs the old position_type
    components: tuple[Component, ...]

@dataclass(frozen=True)
class TemporalExpression:       # table: temporal_expression (HEADER, decision 15;
    kind: TemporalKind          #   stated offsets are component parts — R13/R28)
    referential: TemporalReferential
    temporal_role: TemporalRole | None
    granularity: str | None
    temporal_source: str | None      # clock mechanism: NTP, internal_clock, ...
    components: tuple[Component, ...]

@dataclass(frozen=True)
class SpatialIntegration:       # table: spatial_integration (private, 1:1)
    method: str                 # rule + version + reference data
    latitude_wgs84: float | None
    longitude_wgs84: float | None
    altitude_m: float | None
    # + the metric/motion block exactly as today's Spatial family
    #   (accuracies, speeds, heading, beam_*) — unit conversion IS the integration

@dataclass(frozen=True)
class TimespanIntegration:      # table: timespan_integration (private, 1:1)
    method: str
    lower_unix_utc_us: int | None
    upper_unix_utc_us: int | None
    utc_offset_hours_lower: float | str | None   # applied; UTC_OFFSET_UNKNOWN sentinel
    utc_offset_hours_upper: float | str | None
    time_accuracy_us: int | float | None

@dataclass(frozen=True)
class AssertionLinks:           # columns on: assertion (the four-way triptych)
    entity_role: EntityRole | None
    entity_position_link: EntityPositionLink | None
    entity_time_link: EntityTimeLink | None
    spatial_temporal_link: SpatialTemporalLink | None
```

`column_cast` / `CANONICAL_UNIT` / `unit_factor` survive unchanged — they now describe
the integration tables' columns, which is where unit conversion always conceptually
belonged.

## Cross-check against the old 45-column CSV

Every `OUTPUT_COLUMNS` member, its new home, and whether the change is deliberate:

| old column(s) | new home | verdict |
| --- | --- | --- |
| entity, entity_type | entity.identifier (normalized) / .identifier_type + .type | **better** — type is a vocabulary; 'device' legal and unmergeable; verbatim in raw_json (decision 17) |
| linked_entity | entity.concerns (one value, fixed at parsing; concerns_origin records who specified it) | ✔ resolved F1, carrier changed by decision 17 |
| time_lower/upper_raw, *_source_field, temporal_source | temporal_expression | ✔ same data, now labelled evidence |
| time_lower/upper_unix_utc_us, time_accuracy_us | timespan_integration | ✔ now method-stamped |
| utc_offset_hours (one column) | stated_* (L1, ×2) + applied utc_offset_hours_* (L2, ×2) | **better — see finding F2** |
| latitude/longitude_wgs84, altitude_m | spatial_integration | ✔ derivation documented |
| lat/long_source_field, raw_position, position_source | spatial_expression | ✔ same data, now labelled evidence |
| position_type | spatial_expression.spatial_role | **merged** — its semantics (origin, destination, …) always were the spatial role; referential is the encoding/CRS (resolved F3) |
| accuracies, speeds, heading_*, beam_* (11 cols) | spatial_integration | ✔ deliberate: unit conversion is the integration; raws survive in raw_json |
| entity_position_link, entity_time_link, spatial_temporal_link | assertion | ✔ + entity_role joins them |
| raw_source_path | source.data_source, falling back to the input source at projection (decision 24) | ✔ the V1 hierarchy reinstated at source grain: input source ≠ data source |
| source_label | assertion (the one per-claim provenance field, owner's ruling) | ✔ |
| source_tier | source | ✔ moved to source grain (decision 24) |
| input_file_path, preset_id, preset_name | source | ✔ |
| input_record_id | raw_record | ✔ |
| source_record_uid | raw_record_id (surrogate; the artefact's own key → native_record_uid) | changed on purpose — values regenerate once |
| row_uid | assertion_id | **better** — actually unique now (the founding defect); content_uid added alongside |
| deleted | raw_record.recovery_state | unified on purpose (one concept, one column) — see finding F4 |
| record_type, record_rank | analysis table (per assertion + method version) | changed on purpose — analysis exiled from evidence |

Not in the CSV but stored today and still stored: container_chain, format, subunit,
fingerprint (source); raw_json (record, feeds the orig_ passthrough unchanged);
attribution_origin → superseded by entity_relation.origin. Genesis stays derived,
never stored. New columns with no old counterpart (proposed CSV additions):
`content_uid`, `entity_role`, `activity_id`, `counterpart`, plus the two roles and
referentials if wanted.

### Findings (the things the cross-check caught)

- **F1 — RESOLVED (owner): exactly ONE linked entity per assertion, specified at
  parsing.** Whether it came from `--linked-entity` or a preset mapping, the pipeline
  resolves precedence at parse time (as today) and stores a single
  `assertion.linked_entity_id` → entity mention, with `linked_entity_origin`
  (operator_param | preset_mapped) recording who specified it. Excluded from
  content_hash, so operator input never moves the evidence digest. The relation graph
  remains the separate, optional home of person chains and validity intervals — it
  never produces the CSV column.
- **F2 — RESOLVED (owner): two CSV columns.** `utc_offset_hours_lower` and
  `utc_offset_hours_upper` replace the single `utc_offset_hours` in the flat output.
- **F3 — RESOLVED (owner): no overlap — position_type merges into spatial_role.**
  Its semantics (origin, destination, …) always were the place's role in the activity;
  `referential` is strictly the encoding/CRS (epsg:4326, geohash, address, cell_ref…).
  The column disappears; the CSV projects spatial_role.
- **F4 — deleted ≡ recovery_state assumes no preset maps them independently per
  fan-out row.** No known preset does; if one appears, add a nullable
  assertion.deleted override. Accepted risk, recorded here.

## Open items before code

1. Complete the link vocabularies where epistemic weight now rests (negation beyond
   NEVER_DURING_INTERVAL; reported-by-whom pairs with entity_role=reporter).
   *Partially executed at T10*: `NEVER_DURING_INTERVAL` now also exists on
   `EntityTimeLink` (the demo's alibi stores it there) with a derived
   `Genesis.NEGATED` — **owner reservation (2026-07-15): its presence on the E-T
   edge is to be re-judged against real use cases at the end; keep until then.**
2. ~~locates~~ **resolved (decision 11)**: derived from EntityPositionLink, dropped
   from the diagram.
3. Cross-check against the old 45-column CSV: **done and fully resolved** (F1 →
   decision 12; F2 → two CSV offset columns; F3 → position_type merges into
   spatial_role). Remaining: fix the new flat column list (content_uid, entity_role,
   counterpart, activity_id, the two offset columns) and regenerate goldens once,
   deliberately.
4. Deferred by design: resolved-place layer, cross-record activity linking, plural
   integration candidates (drop the UNIQUEs), method as a registry table if the text
   field proves too loose.
5. The review record is [model_review_findings.md](model_review_findings.md)
   (R1–R26 statuted; R27–R42 from the second review — several mooted by decisions
   16–18, see its supersession note; the survivors to execute: R27 digest recipe in
   the demo, R28 stated-offset components, R30 flat column list, R31 plural-
   integration ruling, R32 vocabulary consolidation, R34 assertion UNIQUEs in the
   diagram, R36 sizing restate, R37–R42 sweeps).
6. The worked examples and the demonstration database predate decisions 16–20 and
   must be regenerated against this shape before Phase 1 treats them as fixtures.
