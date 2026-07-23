# Processing conventions catalogue

Every deliberate, hard-coded processing rule mATLAS applies, from ingest to export. When an
analyst sees a value that looks strange — an offset of `N/A`, two "identical" files treated as
two sources, a blank cell where a number was expected — this is where to confirm whether it is
intended and why.

Conventions are grouped by pipeline phase. Each entry states **what** the rule is, **where** it
applies, and **why** it exists.

---

## 1. Source identity & packaging

**A source's identity is its path, not its bytes.**
A source is identified by the triple *(input file path ⟂ inner path within any container ⟂
sub-unit)* — never by a content hash. Two byte-identical exhibits at two different paths are two
distinct sources; the content fingerprint is recorded as an indexed attribute, not as identity.
*Why:* the same file legitimately appears in different acquisitions/locations and must remain
separately attributable; identity by content would silently merge them.

**Packaging independence ("a file in a zip is the same source as in a folder").**
The same evidence bytes produce identical content and claim digests whether read from a plain
folder or from inside a zip. No path component feeds either digest, so a folder run and a zip run
of the same evidence agree on every evidence identity ("folder == zip" parity).
*Why:* how evidence happens to be packaged for delivery must not change its identity.

**A nested file's location is a chain joined with `!`.**
A file found inside a container is located as `archive.zip!inner.csv` (and deeper, chained). This
chain is part of the source's inner-path identity.

**Archive re-processing is skipped only on explicit opt-in, and only for an identical preset.**
A recognised archive is re-opened every run by default. Skipping a previously-processed archive
happens only when the operator opts in AND the preset signature (key + version + definition
digest) is identical to the earlier run. A skip is always recorded. A preset edited without a
version bump changes its definition digest and therefore forbids the skip.
*Why:* no silent loss — a skip is a deliberate, recorded, provably-safe optimisation, never a
default that could drop evidence.

**A mATLAS case database offered as evidence input is refused.**
A persisted `*.matlas.sqlite` (identified by the SQLite application id plus the `matlas_meta`
marker) fed as the top input is rejected outright; one found nested inside a folder/archive is
flagged and skipped rather than read.
*Why:* a run database holds derivations, not evidence — re-ingesting it as evidence would launder
derived values back into the verbatim layer.

---

## 2. Assemble — Layer 1 (verbatim, no conversion)

**Layer 1 stores source values verbatim; every conversion is a method-stamped Layer-2 row.**
Text extraction is allowed at L1 (it selects verbatim characters from the source value); unit,
format, and type conversions are not — they belong to Layer 2, where a method names them.
*Why:* the verbatim layer must be a faithful, un-transformed record of what the source said, so a
reviewer can always see the original value and audit every derivation separately.

**One output line = one placement.**
A party earns its own output row only when the record gives it its own position (spatial and/or
temporal). Other parties in the same record attach as context (facets, counterparts, mentions),
never as extra rows. A call is two placements (each leg has its own position); a 20-recipient
group message is one placement.
*Why:* a "line" means a located event, not a mention — inflating lines per mentioned party would
misrepresent how many placements the evidence actually supports.

**NULL, `N/A`, and empty are three different things.**
A model field a preset never maps is absent (**NULL**). A preset mapping that evaluates but yields
nothing produces the literal text **`N/A`**. A bound with no instant yields an **empty** cell.
NULL = "never mapped"; `N/A` = "mapped, produced nothing"; empty = "no value here". A template
with *all* placeholders missing renders `N/A`; with *some* missing, those placeholders render
empty.
*Why:* an analyst must be able to tell "we never looked for this" from "we looked and it was not
there" — conflating them hides whether an absence is a gap in the preset or in the evidence.

**Multiplicity absent means one.**
An assertion with no multiplicity recorded represents a single occurrence.

---

## 3. Integrate — Layer 2 (method-stamped derivations)

**Every derived value names the method that produced it.**
Each L2 value carries a method string of the form `rule/version` (optionally `/reference`). A
pass-through counts as a method (e.g. a GNSS latitude that needs no conversion is stamped with a
passthrough method).
*Why:* two values that look identical (a GPS fix, a geocoder centroid, a cell-tower lookup) must
never be indistinguishable — the method makes each derivation's origin explicit and auditable.

**Time offsets have three states.**
- A **number** (e.g. `+02:00`): the source's zone was known and applied.
- **Empty / none**: the source was absolute UTC (an epoch), so nothing was localised and the ISO
  string reads `+00:00`.
- The literal **` N/A`** (trailing): the source was a local wall-clock time with **no** known
  zone; it was materialised by *assuming* UTC and is flagged with the ` N/A` suffix so the
  assumption is visible.

A bound with no instant produces **empty** date/time cells (empty is "no value"; ` N/A` is "a
value whose zone is unknown" — never conflate them).
*Why:* zone handling is where timestamps silently go wrong; making the three cases distinct lets a
reviewer see exactly how each instant was localised.

**Excel serial dates are treated as assumed-UTC.**
An Excel serial date carries no zone, so it is handled like any other zone-unknown local clock.

**The spatial index is the level-30 S2 leaf cell, stored signed.**
A located row gets an S2 cell id at level 30 (the leaf level), stored as the signed
reinterpretation of the unsigned 64-bit id (SQLite integers are signed). A row with no
coordinates gets no index.
*Why:* a single leaf-cell integer per row supports fast spatial proximity queries via id ranges
without a spatial extension; the signed reinterpretation is a storage detail of SQLite's integer
type, not a change of value.

---

## 4. Entity normalisation

**Refuse rather than guess.**
When a raw identifier cannot be safely canonicalised, the normaliser **refuses**: the canonical
identifier is left NULL and the claimed type is untouched, so nothing can converge on it. The
verbatim value and the preset's type claim survive in the hashed L1 layer, so a refused row's
digests are unchanged.
*Why:* a wrong canonical form silently merges two different subjects — the exact failure refuse
semantics exist to prevent. Convergence must be earned, never guessed.

**Convergence is always a query, never a stored merge.**
Two entities are "the same" only as the result of grouping by *(type, canonical identifier)* at
read time. The tool never deduplicates, merges, or deletes an entity row.
*Why:* evidence rows are observations; collapsing them at write time would destroy the record of
what each source independently said.

**Per-type canonicalisation rules (each refuses on failure):**
- **Phone number (msisdn):** canonicalised to E.164 (`+<digits>`); requires country context (an
  explicit `+` or a preset region), else refuses — prefixing a *guessed* country code would
  invent a subscriber.
- **IMEI:** the 14-digit body (the check digit and any software-version digits are dropped);
  anything else refuses.
- **IMSI:** separators stripped to a 6–15 digit string.
- **Email:** lower-cased; refuses anything without exactly one `@` and a dotted domain.
- **IP address:** only globally-routable unicast is kept; private ranges, carrier-grade NAT,
  loopback, and multicast all refuse — two hosts behind the same private range are not the same
  endpoint.
- **MAC address:** lower-case colon form, **universally-administered unicast only**. The
  locally-administered address (which covers modern randomised MACs) and any multicast/group
  address refuse.
- **Vehicle plate:** `<COUNTRY>:<BODY>`, the country taken from an explicit `CC:` prefix in the
  value or the preset's default region; with neither, refuses (an unqualified plate collides
  across countries).

---

## 5. Analyse & concomitance

**Analysis is about assertions, never inside them.**
Analysis rows (e.g. duplicate-record ranking) reference assertions from the outside; they never
edit an assertion. Re-running analysis with a **new** method version adds rows; re-running the
**same** version refuses to overwrite. Every assertion gets an analysis row — an empty verdict
means "examined, not a duplicate", not "unexamined".
*Why:* derived judgements must be separable from evidence and reproducible; a missing row would be
ambiguous between "not looked at" and "nothing found".

**Concomitance is a presence question.**
"Who was together" counts presence: referenced-only pins and negated ("never during") links are
excluded; unmapped/unknown links are *included* (unknown is not evidence of non-presence). An
identity is never reported as together with itself.
*Why:* presence and reference are different claims — a place merely *mentioned* is not a place
someone *was*.

---

## 6. Export

**One file per format, always.**
Every exporter writes a single file per run — one CSV, one KML, one GeoJSON, one workbook, one
database. There is no merge/split mode: the `preset_id` column (or attribute) separates presets
within the one file for a caller who wants to partition them.
*Why:* a predictable single artefact per format is simpler to consume and to reference; the split
was redundant once every row carries its preset.

**The flat CSV is the default projection; other exports are opt-in.**
A run writes the flat assertion CSV unless the operator selects otherwise; additional formats are
requested explicitly. Every export is a projection of the **whole case**, not just the latest run.

**Encoding: BOM for spreadsheet formats, none for the web formats.**
CSV and Excel are written UTF-8 **with** a byte-order mark (so Excel/Notepad auto-detect the
encoding and render accented text correctly). GeoJSON and KML are plain UTF-8 with **no** BOM (the
GeoJSON specification forbids it, and a BOM breaks the KML XML declaration).

**Four provenance/derivation columns are always present.**
`spatial_referential`, `locates`, `spatial_method`, and `temporal_method` appear on every flat-CSV
row, so a GPS fix, a geocoder centroid, and a cell lookup can never render identically.

**The source-path column is a source-grain attribute, outside the evidence digests.**
The raw source path shown in the projection is a property of the source, not of any claim, so it
sits outside both the content and claim digests (which is what keeps folder == zip parity).

---

## 7. Case identity & the append model

**One case per database.**
A `*.matlas.sqlite` holds exactly one case: a case identity, then the runs accumulated against it,
their sources, and everything derived. The case database is the durable artefact; every other
export is a throwaway projection of the whole case.

**A run creates a case or appends to it.**
If the target case database does not exist, the run creates it and mints the case identity. If it
already exists, the run **appends**: a new run row plus its sources/assertions are added under the
existing case identity (read from the database, never re-derived).

**Append is deterministic by construction.**
No run or case identifier feeds any evidence digest, so the same evidence produces byte-identical
content and claim digests whether it lands in the first run or the fifth. Building a case in one
run and building it by appending across several runs yield the identical set of evidence
identities.
*Why:* a row's identity must not depend on *when* it was added, or an incrementally-built case
would disagree with a one-shot build of the same evidence.

**Re-appending evidence already in the case is a recorded skip.**
A source already present in the case (same path identity, from an earlier run) is skipped and the
skip is recorded — never silently dropped and never a crash. A fully-duplicate re-append is a
documented no-op run; a partial overlap ingests only the genuinely new sources. (A duplicate
*within a single run* still fails loudly, since the discovery step should have prevented it.)
*Why:* an operator re-running the same command, or adding a folder that overlaps the case, must
get a safe, legible result rather than a collision.
