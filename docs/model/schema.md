# Run database schema — the settled shape

This is the implementation shape of [conceptual_model.md](conceptual_model.md); the editable ER
diagram is [matlas_run_db.xml](matlas_run_db.xml) (open at <https://sql.toad.cz/>) and the
identity discipline is [identity_model.md](identity_model.md). Design priority: **fidelity to
reality, not performance — and only what is really needed.** [entity_model.md](entity_model.md)
describes the **deferred** analyst layer, not the run schema.

SQLite, **temp on-disk** during the run (`journal_mode=OFF`, `synchronous=OFF` — the
run DB is reproducible scratch), persisted via `VACUUM INTO` (this removes the
old backup-deadlock constraint). `PRAGMA foreign_keys = ON`. `core/vocabularies.py` stays the
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
v1** — designed in entity_model.md, absent from the run schema.

## Tables (by layer)

### Layer 0 — acquisition (verbatim, immutable, never merged)

| table | columns | keys / notes |
| --- | --- | --- |
| case_metadata | case_id, case_number, title, description, created_at | **The root** — uuid4 PK; exactly ONE row per run-DB file (one case per database). `case_number` is the user-facing identifier. Hierarchy: case → run → source → raw_record → assertion. Physical name `case_metadata` (owner ruling 2026-07-14: `CASE` is an SQL keyword; columns keep the `case_` prefix) |
| run | run_id, case_id FK NOT NULL, input_path, started_at, finished_at, tool_version, settings_json, examiner | PK run_id (**uuid4** — the only non-deterministic id; feeds no child id); `case_id` FK; input_path = the operator's `--input` root; **settings_json is normatively COMPLETE**: every operator argument and option, canonical JSON — the run is reproducible from its row; selected presets via `run_preset`; examiner optional |
| matlas_meta | schema_version, families_version (one row) + `PRAGMA user_version` + SQLite `application_id` | versioning and the self-ingestion stamp: discovery refuses/flags stamped artefacts |
| archive | archive_id, path, cd_sha256 | **One row per container path** — id from the **path only**, UNIQUE(path), `cd_sha256` a plain INDEXED column. "Same archive bytes twice" (two identical-bytes exhibits, or across runs) is a **query/finding** (`GROUP BY cd_sha256`). `cd_sha256` **reuses `model_atlas/sqlite/integrity.py` (`fingerprint_zip` / `zip_resume_key`)** — no new hashing |
| run_archive | run_archive_id, run_id FK, archive_id FK, parsed_at, skipped | **The run↔archive link**: one run opens many archives, one archive may appear in many runs — UNIQUE(run_id, archive_id). EVERY encounter is recorded, including an **opt-in skip** (`skipped=1`, never traceless): re-walking may be skipped only when the operator opted in (recorded in settings_json) AND a previous run parsed the same `cd_sha256` with the same preset set (key + version + definition_digest) |
| run_preset | run_preset_id, run_id FK, preset_id FK | **The run↔preset link**: every preset SELECTED for the run (reusing the preset table), recorded even when a preset yields no assertion. UNIQUE(run_id, preset_id) |
| source | source_id, run_id FK, input_file_path, inner_path, data_source, format, subunit, fingerprint_sha256, sibling_fingerprints_json, exhibit_ref, source_tier, record_count | UNIQUE(run_id, input_file_path, inner_path, subunit); INDEX fingerprint_sha256 — the id derives from **paths, never the fingerprint**. **Preset-free**: a source is *what was opened* (folder→file, zip→inner file); one file matching several presets is ONE source row and ONE set of raw_records — the reading lives on the assertion. `inner_path` = chain-inclusive container path; `data_source` = where the data originally lived (UFDR/tool manifest device path), NULL for primary acquisitions. Fingerprint = main file bytes; siblings in sibling_fingerprints_json; exhibit_ref |
| preset | preset_id, key, name, version, definition_digest | the companion table: one row per preset applied in this run — uuid5(key ⟂ version), a config row whose definition IS its identity (same argument as the old method registry). UNIQUE(key, version). **definition_digest**: sha256 of the canonical preset definition — catches edits the author forgot to version-bump (same key+version, different digest across runs = a surfaced finding; skip eligibility compares digests, never just versions) |
| raw_record | raw_record_id, source_id FK, line_no, native_record_uid, recovery_state, recovery_source, raw_json | UNIQUE(source_id, line_no); raw_json is the COMPLETE source row. `recovery_source` is the fine origin (live/wal/journal/freelist/freespace) behind the coarse `recovery_state`, ordered by descending reliability so it doubles as the confidence signal; digest-excluded provenance, surfaced per claim via the raw_record→assertion join |

#### The source hierarchy (reinstated; grain fixed by the owner)

The provenance columns all survive — **each at its natural grain, duplicated
nowhere** (owner's ruling: the fields must exist, not be repeated per assertion):

| original CSV column | grain | home | projected from |
| --- | --- | --- | --- |
| (new) | run | run.input_path — the operator's `--input` root | run |
| input_file_path | source | source.input_file_path (outermost artefact opened) + inner_path, format, subunit | source |
| raw_source_path | source | source.data_source — where the data originally lived (UFDR/tool manifest device path); NULL for primary acquisitions | data_source, falling back to input_file_path!inner_path |
| source_tier | source | source.source_tier: primary (acquisition parsed) vs secondary (tool export parsed, interpretation inherited) | source |
| preset_id, preset_name | **assertion** (via the preset companion table) | assertion.preset_id → preset | preset |
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
| activity | activity_id, kind, context_json | one real-world happening. **context_json**: preset-mapped, semi-structured activity context — the callee, the caller's IMEI, the recipients list — canonical JSON, inside the L1 digest projection, projected to the flat counterpart/facets columns. **Bonus table**: it only groups the legs of one record; everything exploitable reads from entity/links/roles, and no mandatory CSV column sources from it |
| activity_record | activity_record_id, activity_id FK NOT NULL, raw_record_id FK NOT NULL | UNIQUE(activity_id, raw_record_id). **The activity↔record binding is a link table, not a column.** The activity↔record binding is a link table, not a column — ingest writes exactly one row per activity (its own record; the mis-attachment guard becomes an ingest invariant + lint); an *analysis* act may later attach further records to the same activity ("both operators' records describe this call"), which replaces the deferred activity_link table with something simpler |
| assertion | assertion_id, raw_record_id FK NOT NULL, **preset_id FK NOT NULL**, entity_id FK NOT NULL, entity_origin, activity_id FK NULL, spatial_expression_id FK NULL, temporal_expression_id FK NULL, entity_role, entity_position_link, entity_time_link, spatial_temporal_link, deleted, multiplicity (NULL = 1), source_label, content_hash, claim_digest | **UNIQUE(raw_record_id, preset_id, output_ordinal)** — two presets reading the same record produce distinct assertions; UNIQUE on each corner FK (owned corners) and on entity_id; both digests indexed, repeat legally, never keys. linked_entity dropped — entity.concerns; data_source/source_tier at source grain; source_label = the one per-claim provenance field |
| entity | entity_id, identifier, raw_identifier, identifier_type, type, concerns, concerns_origin, concerns_uid | **One lean row**: `identifier` = **normalized at ingest** (E.164, lowercase email, …; NULL for a purely typed mention); `raw_identifier` = **as written in the source, verbatim**; `identifier_type` names which normalization applied (msisdn \| imei \| email \| plate \| account \| other \| none); `type` = what the thing is (device \| person \| vehicle \| camera \| …); `concerns` = the attributed party fixed at parsing (absorbs linked_entity), `concerns_origin` (operator_param \| preset_mapped), `concerns_uid` = digest of the case-folded/stemmed concerns. Occurrence row, no FK, no dedup; convergence = GROUP BY (identifier_type, identifier); attribution grouping = GROUP BY concerns_uid. Normalization + stemming rule-set versions recorded once per run in matlas_meta |
| spatial_expression | spatial_expression_id, referential, spatial_source | HEADER only: referential = encoding/CRS (epsg:4326 \| geohash \| address \| place_name \| cell_ref \| asset_ref \| region); spatial_source = the mechanism (GNSS \| WiFi \| cell \| …, the position source, symmetric with temporal_source). The values live in component rows; **spatial_role moved to the component** |
| spatial_component | spatial_component_id, spatial_expression_id FK, ordinal, part, spatial_role, source_field, raw_value | UNIQUE(spatial_expression_id, ordinal). One row per source field, verbatim. **`part` uses the PRESET target-field names**: raw_position (composite) \| latitude_wgs84 \| longitude_wgs84 \| altitude_m \| horizontal_accuracy_m \| vertical_accuracy_m \| horizontal_speed_kmh \| vertical_speed_kmh \| horizontal_speed_accuracy_kmh \| vertical_speed_accuracy_kmh \| heading_deg \| heading_accuracy_deg \| beam_azimuth_deg \| beam_width_deg (+ zone/easting/northing for gridded CRS). Any part value is accepted; only standard names are export-projected. **spatial_role per component**: vicinity \| entry \| exit \| origin \| destination \| waypoint — a value-group can carry its own role. A 1-field geohash is 1 row; lat+lon columns are 2; DMS is 6 |
| temporal_expression | temporal_expression_id, kind, referential, temporal_source | HEADER only; referential: unix_epoch_s \| apple_cfabsolute \| iso8601 \| excel_serial \| local_string \| …; temporal_source = clock mechanism. **temporal_role and the stated offsets moved to the component; granularity dropped |
| temporal_component | temporal_component_id, temporal_expression_id FK, ordinal, part, temporal_role, stated_offset, source_field, raw_value | UNIQUE(temporal_expression_id, ordinal). **`part` uses the PRESET field names**: lower \| upper \| duration (+ lower_date/lower_time-style splits when a bound spans columns). **Per-component**: `temporal_role` (start \| end \| booking \| check_in \| check_out \| last_seen — each bound carries its own role: a visit's lower is check_in, its upper check_out) and `stated_offset` (the offset the source stated for THIS bound, verbatim TEXT; the dual offsets are now per-bound columns, not parts). A date column + time column stay two verbatim rows; the integration combines them (datetime_combine) |

**The four link vocabularies** live on the assertion and absorb epistemic status
(`REPORTED_FOR`, `CLAIMED_AT`, `NEVER_DURING_INTERVAL` are already members):
`entity_role` (caller, callee, sender, recipient, reporter, subject…) — the entity's
part in the activity; the three triangle links as today. `spatial_role` / `temporal_role`
(origin, destination, vicinity, entry… / start, end, booking, check_in…) sit on the
expressions — owned 1:1, so equivalent to assertion columns, and each corner stays
self-describing.

### Layer 2 — integration (derived, private, method-stamped)

The integration tables are **the modelled space+time layer**: every derived
spatial/temporal measurement column the flat CSV exposes lives here, so the CSV's space and
time block is a pure projection and integration re-runs (new geocoder / tz-db / S2 level)
without touching L1. The CSV's *other* columns are **not** duplicated here — links / roles /
deleted / multiplicity stay on `assertion`, identifier / type / concerns on `entity`,
paths / tier / preset / data_source on `source`, input_record_id on `raw_record`.

| table | columns | keys / notes |
| --- | --- | --- |
| spatial_integration | spatial_integration_id, spatial_expression_id FK, method, latitude_wgs84, longitude_wgs84, altitude_m, horizontal/vertical_accuracy_m, the 4 speed columns, heading_deg, heading_accuracy_deg, beam_azimuth_deg, beam_width_deg | UNIQUE(spatial_expression_id) in v1 (drop the UNIQUE to allow candidates later); the unit conversion IS the interpretation, raws survive in raw_json. This is the complete derived-spatial block |
| spatial_index | spatial_index_id, spatial_integration_id FK UNIQUE, s2_cell_id, s2_level | **S2 index**: one row per point (per spatial_integration); `s2_cell_id` = the signed-int64 **leaf** S2 cell (deliberately **non-unique** — many points share a cell), INDEXED. Hierarchy is implicit in the id (ancestors = bit-truncations; descendants = a contiguous `[range_min, range_max]`), so concomitance queries do `WHERE s2_cell_id BETWEEN ? AND ?` on the index — no parent/child table. Built with `s2sphere` (`CellId.from_lat_lng`); level-walking lives in the concomitance engine. Optional precomputed ancestor-level columns (e.g. `s2_cell_l12`) can be added later for SQL binning, no redesign |
| timespan_integration | timespan_integration_id, temporal_expression_id FK, method, lower_unix_utc_us, upper_unix_utc_us, lower_datetime_iso, upper_datetime_iso, lower_date, upper_date, lower_time, upper_time, utc_offset_hours_lower, utc_offset_hours_upper, lower_accuracy_us, upper_accuracy_us | UNIQUE(temporal_expression_id); applied offsets per bound incl. the `N/A` assumed-UTC sentinel. `*_accuracy_us` = the per-bound timing uncertainty (whole µs), split like the offsets — a source-stated value or the Method's own claim, empty when neither. **Convenience projections**: `*_unix_utc_us` = canonical UTC µs; `*_datetime_iso` = ONE offset-tagged **local** ISO string (`…+01:00`; when zone unknown render the assumed clock with a loud trailing ` N/A`; empty when there is no instant); `*_date` / `*_time` = the local date / time-of-day (µs). Derived from unix µs + applied offset. Empty = no value; ` N/A` = value, zone unknown (kept distinct) |

`entity_integration` is **gone**: the entity corner trades its L1/L2
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
| analysis | analysis_id, assertion_id FK, method, record_type, record_rank | UNIQUE(assertion_id, method): the generic, method-attributed verdict table. Re-running a changed ranker ADDS rows, never mutates; the CSV projects the current method's verdict |
| edit_log | edit_id, ordinal, table_name, row_id, column, old_value, new_value, edit_kind, edited_by, edited_at | **Manual-edit audit**, for the edit panel. `edit_kind` ∈ update \| new_row \| empty_row. **Evidence protection**: L0/L1 rows (case/run/source/raw_record/assertion/entity/expressions/components) are NEVER updated in place — only `new_row` (mint a new occurrence, keep the original) or `empty_row`; integration/analysis rows may `update` in place. Every action logs one row here; the edit API in `model_atlas` enforces the protection, not the UI. **Full rewind**: `ordinal` is a monotonic UNIQUE journal position; `update` logs per-column old→new, `new_row`/`empty_row` log the FULL row snapshot (canonical JSON in new_value) — applying inverses in reverse ordinal order restores any earlier state; a rewind appends inverse entries, never erases the journal |

**Deferred out of v1**: `resolved_entity`, `entity_relation`,
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
| case_metadata | occurrence | **uuid4** (one per DB) |
| run | occurrence | **uuid4** |
| archive | occurrence | path |
| run_archive | occurrence | run_id ⟂ archive_id (run metadata, like the run row itself — the diffability rule exempts run-scoped links); UNIQUE(run_id, archive_id) |
| run_preset | occurrence | run_id ⟂ preset_id (run metadata); UNIQUE(run_id, preset_id) |
| source | occurrence | input_file_path ⟂ inner_path ⟂ subunit (preset removed) |
| preset | **content** (config) | key ⟂ version — a preset's definition is its identity |
| raw_record | occurrence | source_id ⟂ line_no |
| assertion | occurrence | raw_record_id ⟂ preset key ⟂ output_ordinal |
| activity | occurrence | raw_record_id ⟂ activity_ordinal (per-record at ingest) |
| activity_record | occurrence | activity_id ⟂ raw_record_id; UNIQUE(activity_id, raw_record_id) — ingest writes exactly one per activity |
| entity / spatial_expression / temporal_expression | occurrence | assertion natural key ⟂ corner tag |
| spatial/temporal_component | occurrence | expression_id ⟂ ordinal; UNIQUE(expression_id, ordinal) |
| the two integrations | occurrence | expression_id ⟂ method (⟂ candidate_ordinal when plural arrives) |
| spatial_index | occurrence | spatial_integration_id (1:1) |
| analysis | occurrence | assertion_id ⟂ method |
| edit_log | occurrence | uuid4 (an audit event) |

The only content-keyed table is `preset` (config: key⟂version). Every other table is an
occurrence key — the last identity content key (`resolved_entity`) left the run schema with
the deferred analyst graph.

**The normative L1 projection** — the
exact field list both digests hash, in order:
entity (identifier ⟂ identifier_type ⟂ type ⟂ entity_role) ⟂ spatial expression
(presence-flag ⟂ [referential ⟂ spatial_source ⟂ component-count ⟂ components ordered
by ordinal, each as part ⟂ spatial_role ⟂ raw_value]) ⟂ temporal expression
(presence-flag ⟂ [kind ⟂ referential ⟂ component-count ⟂ components likewise, each as
part ⟂ temporal_role ⟂ stated_offset ⟂ raw_value]) ⟂ the three links ⟂ deleted ⟂
multiplicity ⟂ activity (presence-flag ⟂ [kind ⟂ canonical context_json]).
(data_source is source-grain — and therefore outside the claim-level digests, like
every other source attribute.)

The projection is **injective** (no two structurally-different claims share an L1
string) by two rules working together: (a) values are rendered SEP-safe — a value's
literal `\x1f` (or the `\x1b` escape introducer) is escaped, so a bare `\x1f` is always
a delimiter and the token stream is uniquely recoverable; (b) every variable-length
section is **framed** — the entity block is a fixed 4 fields and the links+flags block a
fixed 5, and each optional corner leads with a presence flag (present / absent) followed,
for a present spatial/temporal corner, by an explicit **component count** that pins how
many component rows follow. So a component can never be shifted across the
spatial/temporal boundary, and an **absent corner** (flag only) can never render like a
**present-but-empty corner** (flag + empty fields + count 0). Before this framing the
projection was non-injective: unframed component lists joined by the same separator, and
`_txt(None) == _txt("")` conflated the two corner cases — either could forge a
same-claim/corroboration collision.
**Excluded, normatively**: `concerns`, `concerns_uid` + both origin columns (operator
input and derived groupings never move an evidence digest), `raw_identifier`
(format-variant; the normalized identifier already represents the mention — a
re-disclosure with different formatting must converge), component `source_field`s
(likewise for renamed columns), all input paths, all `*_id`s and run_id, all
integrations (register/tzdb upgrades must not move the digest), all analysis rows.

**`assertion.content_hash` (CSV `content_uid`)** = fingerprint_sha256 ⟂ preset key ⟂
line_no ⟂ output_ordinal ⟂ the L1 projection — the digest **scoped to the preset's
reading of this file**, now literally;
folder==zip parity holds: no paths. `claim_digest` stays preset-free: two presets
extracting the same claim from one record legitimately converge there.

**`assertion.claim_digest`** = the L1 projection **alone** (no fingerprint, no
line_no, no output_ordinal). Two overlapping disclosures — different CDR files
sharing 3 000 records — converge here and surface as a *possible same-evidence*
finding instead of being presented as corroboration. Both digests indexed; both
repeat legally; neither is ever a key. Both use the canonical JSON form defined below.

## Determinism — normative enumeration and canonicalisation

The ids and digests embed `line_no` and `output_ordinal`; diffability is a promise
only if enumeration is pinned. Normative rules, each with a test:

1. **SQLite**: records enumerate in `rowid` order (`WITHOUT ROWID` tables: primary-key
   order). Recovered records (WAL / journal / freelist / freespace) enumerate **after**
   all live rows, in recovery-scan order, continuing the same `line_no` sequence;
   `recovery_state` marks them recovered and `recovery_source` names the exact origin.
2. **CSV**: physical line order; `line_no` = 1-based data-line number (header
   excluded), blank lines counted.
3. **Excel**: sheet row order; `line_no` = the sheet row number, so blank rows shift
   nothing.
4. **Paths** feeding `source_id`: absolute, NFC-normalised, `/` separators; container
   chains joined with `!` (chain-inclusive `inner_path`).
5. **Canonical JSON** (for `raw_json` digests and method parameters): UTF-8, sorted
   keys, no insignificant whitespace, no float re-formatting (values kept as read).
6. **Aggregations in projections** (counterpart, context_mentions, facets): ordered
   by mention ordinal, then entity value — never by rowid.
7. **BLOB policy** (exercised when UFDR ships): a binary cell becomes
   `{"$blob": {"sha256": …, "bytes": n}}` in raw_json; the bytes themselves are not
   duplicated into the run DB.

## The flat view

One CSV line per assertion — an assertion exists only per
*placement*, so the line count is the placement count (a one-legged CDR is 1 line,
never 3; a 20-recipient message is 1 line). `row_uid` = assertion_id; `content_uid` =
content_hash; `entity` = entity.identifier (+ identifier_type, type columns);
`linked_entity` = entity.concerns (origin projected); `counterpart` / `facets` /
further context = projected from **activity.context_json** and, for
detector/infrastructure records, already visible in the spatial expression itself
(the camera id IS the cell_ref/asset_ref component) — **bonus columns**:
of the activity, only `assertion.activity_id` is a useful projection (it regroups the
lines referencing the same activity); no mandatory CSV column sources from the
activity table; everything else the record
carried reaches the CSV via the orig_ passthrough. Coordinates/UTC come from the
(single, v1) integrations, with referential, locates (derived) and the methods as
mandatory columns. No invented positions: an assertion without a spatial
expression yields empty coordinates by construction.

## Ingestion write order

run → source → raw_record → activity (with its context_json) + activity_record link
→ **entity + expressions + components first, then the assertion that references them** (FKs
check immediately) → integrations → *(after ingest)* analysis rows (untangle etc.).
**No `INSERT OR IGNORE` anywhere**; no content-keyed table exists in the run schema —
a UNIQUE violation anywhere is a derivation bug and raises.

## Silent-loss inventory (each no-silent-loss failure → its structural guard)

| failure mode | guard |
| --- | --- |
| two exhibits collapse into one source | source_id from paths, not fingerprint; natural UNIQUE; no OR IGNORE |
| duplicate evidence drops an assertion | assertion keyed by record+ordinal; content_hash never a key |
| two assertions share a corner or an entity row | UNIQUE on each corner FK and on entity_id |
| two phones become one entity | entity rows are occurrences; nothing dedups at ingest; 'device' is a TYPE, identifier NULL — nothing exists to merge |
| register/tzdb upgrade rewrites evidence | integrations keyed by expression+method; digest excludes integrations |
| operator input mutates the digest | concerns + both origin columns excluded from both digests, normatively |
| a context party silently vanishes | it is in raw_json verbatim always; activity.context_json is inside the L1 projection, so dropping it moves both digests |
| an activity attaches to the wrong record | activity_record link: UNIQUE(activity_id, raw_record_id) + the ingest invariant "one link row per activity, its own record" (an ingest invariant + lint; analysis-time additions are separately attributed) |
| a positionless claim vanishes from CSV | flat rule: one line per assertion, corners nullable |
| merging two run DBs collides ids | run_id excluded from child ids + DB-file-is-run-scope rule |
| `\|` in a path forges equal uuid5 inputs | `\x1f` join; PK collision raises as backstop |
| an opt-in archive skip hides evidence untraceably | the skip is recorded (run_archive.skipped=1) and eligible only when a previous run parsed the same cd_sha256 with an identical preset set — key, version AND definition_digest) |
| a preset edit without a version bump changes results silently | preset.definition_digest: same key+version with a different digest across runs is a surfaced finding |

## Deferred by design

- The resolved-place layer, cross-record activity linking, and the analyst graph — see
  [entity_model.md](entity_model.md).
- Plural integration candidates (relaxing the one-integration-per-expression uniqueness) if a
  source ever needs more than one method per value.
- Negation beyond a single "never during" temporal link, if real use cases call for it.
