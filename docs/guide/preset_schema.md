# Preset YAML Schema (v3) — Reference

This is the exhaustive reference for the mATLAS preset language: every admitted
top-level key, every reference kind, every mapping attribute, every pipe transform, and
every controlled vocabulary. For a task-oriented walkthrough with worked examples and
edge-case narrative, read the companion [Preset Authoring Guide](preset_authoring.md);
for the target data model the mappings feed, read the [run database
schema](../model/schema.md).

A preset is a YAML file. Presets are loaded recursively from a preset folder, or loaded
directly when `--presets` points at a single YAML file. A preset selects one source,
extracts its rows, and maps each source row into one or more canonical **observation**
rows (the source's words, Layer 1). No unit conversion, timezone resolution, or datum
adoption happens in a preset — those are method-stamped Layer-2 interpretations. A
preset states only what the source says and how to decode it.

The authoritative surface described here is fixed by the parser in
`model_atlas/presets/spec.py` (structure and the assignable-field gate),
`model_atlas/presets/expr.py` (reference, conditional and pipe grammar),
`model_atlas/presets/pipes.py` (the transform registry),
`model_atlas/presets/value_maps.py` (key/bool normalisation) and
`model_atlas/core/vocabularies.py` (the controlled vocabularies). Nothing outside those
symbols is admitted.

---

## 1. Document shape

```yaml
preset:                                   # header (required)
  id: ios.routined.cached_locations       # stable machine key (required)
  name: Routined Cached Locations         # human name (required)
  os: iOS
  tool:                                   # empty = primary/device source
  os_version: ">=13"
  version: 1.0
  tier: primary

input_selector:                           # required; one mapping OR a list
  format: sqlite                          # required: csv | excel | sqlite | xml
  path: /private/var/.../Cache.sqlite     # XOR name
  table: ZRTCLLOCATIONMO

source_record_uid: field(uuid)            # optional; `default` (or omitted) auto-generates

expected_fields:                          # optional; the declared inventory (globs ok)
  - ZLATITUDE
  - ZLONGITUDE

lookup_tables:                            # optional; name -> {key: value}
  recovery: {Parsing: intact, Carving: recovered}
patterns:                                 # optional; name -> regex (named groups)
  coords: "(?P<lat>-?\\d+\\.\\d+) - (?P<lon>-?\\d+\\.\\d+)"

common:                                   # optional; fields shared by every observation
  entity: param(entity)
  entity_type: const(device)

observations:                               # required; non-empty list
  - position:
      latitude:  field(ZLATITUDE)
      longitude: field(ZLONGITUDE)
      spatial_referential: const(epsg:4326)
    time:
      instant: field(ZTIMESTAMP)
      epoch:   cocoa
      zone:    const(UTC)
    links:
      entity_position:  at
      entity_time:      observed_at
      spatial_temporal: instant
```

## 2. Top-level keys

| Key | Required | Type | Purpose |
| --- | --- | --- | --- |
| `preset` | yes | mapping | The header (see §3). |
| `input_selector` | yes | mapping or list | Where the source is and how to read it (see §4). |
| `source_record_uid` | no | field-spec or `default` | The per-source-record UID; `default` (or omission) auto-generates one (see §13). |
| `expected_fields` | no | list of strings | The declared source-field inventory; drives the drift/frontier report (see §6). |
| `lookup_tables` | no | mapping of name → table | Named value maps for the `lookup` pipe (see §10). |
| `patterns` | no | mapping of name → regex | Named regexes (with named groups) for `extract` and the `regex` pipe (see §10). |
| `common` | no | mapping | Field mappings shared by every observation of a record (see §7). |
| `observations` | yes | non-empty list | One entry per position/time/links grouping (see §7). |

This set is **closed**: any other top-level key is a load-time error. The rejection exists
because a model field written at the top level instead of under `common:` (a stray
`input_record_id:` or `context.antenna:`) used to be read by nobody and dropped in silence —
the mapping looked present and produced nothing.

Any spatial or entity field may be declared once in `common:` instead of repeating it in
each observation; a `common:`-mapped coordinate becomes a spatial component shared by every
observation the record fans out into.

## 3. `preset` header

Parsed by `_parse_meta`. `id` and `name` are required non-empty strings; the rest are
optional.

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable dotted identifier, recorded in traceability and used to derive the preset row id. |
| `name` | yes | Short human name. |
| `os` | no | Platform; composes the title. |
| `tool` | no | Forensic tool (AXIOM, Cellebrite). Empty for a primary/device source. |
| `os_version` | no | Applicability range (`">=13"`, `">=15 <18"`); empty = any version. Used as a tie-break. |
| `version` | no | Preset version (stringified; defaults to `"0"`). |
| `tier` | no | One of the `SourceTier` members: `primary`, `secondary`, `unknown`. Validated at load. |

The title is composed: the non-empty of `os` / `tool` joined by a space, then `— name`
(e.g. `iOS — Routined Cached Locations`, or `iOS AXIOM — Cached Locations`).

## 4. `input_selector`

Parsed by `_parse_input_selectors` / `_parse_one_selector`. It is **one mapping or a
list** of mappings. A folder and an archive are read the same way, so a selector matches
identically in either.

### 4.1 Common keys

| Key | Applies to | Meaning |
| --- | --- | --- |
| `format` | all | **Required**, magic-verified: `csv` \| `excel` \| `sqlite` \| `xml`. |
| `path` | all | Anchored path from the container root. **Exactly one of `path` / `name`.** |
| `name` | all | Basename, matched anywhere in the tree. **Exactly one of `path` / `name`.** |
| `role` | all | Optional, default `source`. Same role = OR; different roles = AND (see §4.4). |

### 4.2 Per-format reader keys

| Key | Format | Meaning |
| --- | --- | --- |
| `table` | sqlite | A table name. **Exactly one of `table` / `sql`.** |
| `sql` | sqlite | A read-only SQL query. **Exactly one of `table` / `sql`.** |
| `sheet` | excel | Sheet name. **Required** for excel. |
| `model` | xml | The model type to extract. **Required** for xml. |
| `namespace` | xml | The dialect's root namespace (the discriminator). Optional. |
| `delimiter` | csv | Field delimiter. |
| `encoding` | csv | Text encoding (e.g. `utf-8-sig`). |
| `header_row` | csv, excel | 0-based index of the header row. |
| `skip_rows` | csv, excel | Rows to skip before the header. |

The four read options (`delimiter`, `encoding`, `header_row`, `skip_rows`) are written as
**flat keys on the selector mapping**, not nested under a sub-key; the parser collects
them into the reader parameters internally.

### 4.3 `path` vs `name`

The distinction is basename-vs-path, not folder-vs-archive.

- **`name:`** matches the basename anywhere in the tree (the easy CSV / tool-export case).
  Matching several files is **fan-out** — the preset applies to each.
- **`path:`** is a full **anchored** path from the container root, validated by
  `ingest/pathmatch.validate_selector_path`, with **prefix tolerance**
  (`--root-prefix-depth`, default 1) so `/private/...` matches `filesystem1/private/...`,
  `_/private/...`, or bare `private/...`. Wildcards apply only to variable segments:
  `{uuid}` (a UUID segment) and `*` (one opaque segment). There is **no `**`** — `path`
  is a full path, not a roaming glob. Use it where location is the discriminator (an iOS
  dump holds many unrelated `Cache.sqlite` files).

**`format` is always magic-verified.** If the file's detected format disagrees with the
declared `format`, the (file, preset) pairing is skipped with a warning in auto mode (a
hard error in force-preset mode).

### 4.4 Multiple selectors

A list of selectors expresses alternatives and requirements by their `role`:

- **Same `role` (or all default `source`) = OR.** The first to match supplies the reader
  parameters.
- **Distinct roles = AND** — every role must resolve. This requires the deferred python
  ScriptExtractor; running one now raises `NotImplementedError`.

When several presets match one file, the engine tie-breaks by **structural fit** — how
well each preset's `expected_fields` match the file's actual fields (falling back to the
mapped fields when no inventory is declared) — then declaration order.

## 5. Assignable model fields

The fields a preset may map are exactly the members of `ASSIGNABLE_FIELDS` in
`presets/spec.py`, plus any `context.<key>` (the free key after the dot names one entry
of the activity's context object). Mapping any other name is a load-time error
(`'<field>' is not an assignable model field`).

### 5.1 Entity

| Field | Target | Notes |
| --- | --- | --- |
| `entity` | `entity.raw_identifier` | The identifier as written in the source (verbatim). The normalised `identifier` and `concerns_uid` are filled later by the entity normaliser. |
| `entity_type` | `entity.type` | Defaults to `unknown` when unmapped. |
| `concerns` | `entity.concerns` | The attributed party (verbatim). |
| `entity_role` | `observation.entity_role` | One `EntityRole` member. |
| `identifier_type` | `entity.identifier_type` | Names the identifier kind; free text (see `IdentifierType` for the recognised kinds). Defaults to `none`. |

### 5.2 Temporal (assignable subset)

The temporal **bounds** are not mapped here — they come from the `time:` block (§11).
These are the extra temporal attributes and per-bound overrides.

| Field | Target |
| --- | --- |
| `utc_offset_hours` | The applied offset (usually set via `time.zone`, not directly). |
| `temporal_source` | `temporal_expression.temporal_source` (the clock mechanism: NTP, internal_clock…). |
| `temporal_referential` | `temporal_expression.referential` (explicit override of the derivation from `epoch`/`format`). |
| `temporal_role_lower` / `temporal_role_upper` | Per-bound `TemporalRole`. |
| `temporal_part_lower` / `temporal_part_upper` | Per-bound component part name (default `lower` / `upper`; e.g. rename to `duration`). |
| `time_accuracy_us_lower` / `time_accuracy_us_upper` | Per-bound source-stated timing uncertainty (µs), written verbatim as a `lower_accuracy` / `upper_accuracy` component. |

### 5.3 Spatial

The two angle parts `latitude` / `longitude` are **datum-agnostic** — they name "the
source's latitude / longitude words". The datum is stated separately via
`spatial_referential` (see §12).

| Field | Target / canonical unit |
| --- | --- |
| `latitude`, `longitude` | Datum-agnostic angle components. |
| `altitude_m` | metres |
| `raw_position` | Composite / free-text position (one component). |
| `position_type` | The place's role → `spatial_component.spatial_role` (`SpatialRole`). |
| `position_source` | The mechanism → `spatial_expression.spatial_source` (GNSS, WiFi, cell…). |
| `horizontal_accuracy_m`, `vertical_accuracy_m` | metres |
| `horizontal_speed_kmh`, `vertical_speed_kmh` | km/h |
| `horizontal_speed_accuracy_kmh`, `vertical_speed_accuracy_kmh` | km/h |
| `heading_deg`, `heading_accuracy_deg` | degrees |
| `beam_azimuth_deg`, `beam_width_deg` | degrees |
| `spatial_referential` | `spatial_expression.referential` (`SpatialReferential`). |

### 5.4 Activity

| Field | Target |
| --- | --- |
| `activity_kind` | `activity.kind` (`ActivityKind`). Mapping it **in `common:`** creates the record's activity; without it no activity row is written. |
| `multiplicity` | `observation.multiplicity` (integer; NULL means 1). |
| `context.<key>` | One entry of `activity.context_json` (e.g. `context.callee`, `context.caller_imei`). The key after the dot is free text. |

### 5.5 Bindings (the three links)

| Field | Target |
| --- | --- |
| `entity_position_link` | `observation.entity_position_link` (`EntityPositionLink`). |
| `entity_time_link` | `observation.entity_time_link` (`EntityTimeLink`). |
| `spatial_temporal_link` | `observation.spatial_temporal_link` (`SpatialTemporalLink`). |

The short `links:` block (§9) is the ergonomic way to set these three with bare enum
tokens. Use the full column names here when a link value must come from a conditional or
another reference rather than a bare token.

### 5.6 Provenance (assignable subset)

| Field | Target |
| --- | --- |
| `raw_source_path` | Legacy alias of `data_source` — the source-grain `data_source` on the `source` row. Resolved once per source. |
| `data_source` | Where the trace originally lived (preferred spelling). |
| `input_record_id` | `observation.input_record_id` — a human locator back to the source record; defaults to `<table-or-sheet>#<ordinal>` when unmapped. |
| `source_label` | `observation.source_label` — one per-claim provenance label. |
| `source_tier` | Accepted and validated against `SourceTier`; the header `tier` is the usual place to state the tier. |
| `deleted` | `observation.deleted` (`RecordState`) — the app's own record-state flag. |

### 5.7 Engine-owned fields (never assignable)

These are set by the engine and rejected in a mapping:

- The temporal bounds — mapped through the `time:` block, not as fields.
- The `latitude` / `longitude` **source field** — auto-captured on the component from the
  reference itself; never mapped separately.
- `input_file_path` (the outermost artefact opened), `preset_id`, `preset_name`,
  `row_uid`, `record_type`, `record_rank`.
- `recovery_state` — captured by every reader and surfaced on the extract, not mappable.

## 6. `expected_fields`

The full source-field inventory the examiner declares **before** mapping. The intended
authoring flow is "list every field you have, then map the subset you need". It powers the
drift / frontier report (present-but-unmapped = the research backlog; declared-but-absent
= drift) and the AXIOM differential.

```yaml
expected_fields:
  - Latitude
  - Longitude
  - "Speed (m/s)"
  - "Timestamp Date/Time - * (dd.MM.yyyy)"   # entries may be exact names OR globs
```

It is optional but recommended; the linter advises when it is missing and warns when the
mapping reads a field the inventory does not cover.

## 7. Field mappings: `common` and `observations`

`common:` is a mapping of `<assignable-field>: <value>` shared by every observation. Each
`observations:` entry is a mapping with:

- `position:` — a mapping of spatial fields plus any scalar fields (links by full column
  name, roles, `deleted`, `multiplicity`, `context.<key>`, …).
- `time:` — the temporal block (§11). **Required** on every observation.
- `links:` — the three edges as bare enum tokens (§9). Optional.

One observation = one position + one time + the three links. For two times at one place
(e.g. a dwell interval and a created instant), write two `observations:` entries; they share
the record's UID automatically.

### 7.1 Value forms

Every mapped value is exactly one of:

1. a single **reference call** (§8),
2. an **`if / elif / else` conditional chain** over reference calls (§8.3), or
3. a **mapping** `{from: <ref-or-conditional>, type:, unit:, extract:, pipe:, optional:}`
   (§8.2).

## 8. Reference vocabulary

Parsed by `expr.parse_ref`. The reference kinds are exactly `REF_KINDS`:

| Form | Value | Argument constraint |
| --- | --- | --- |
| `field(NAME)` | A source field's value. | `NAME` may be a glob; quote it if it contains spaces (`field("Speed (m/s)")`). Must be non-empty. |
| `header("Glob *")` | The matched field's header **text** (e.g. a timezone embedded in a column header). | Same glob rules as `field`. |
| `filename(name\|stem\|path)` | Part of the source file identity: `name` (basename), `stem` (basename without extension), or `path` (full path). | One of `name`, `stem`, `path`. |
| `param(entity\|concerns)` | A run-level argument (`--entity` / `--concerns`). | One of `entity`, `concerns`. |
| `preset(<token>)` | A value from the current preset's own definition (see §8.1). | One of `PRESET_TOKENS`. |
| `const(VALUE)` | A literal. Coerced: quoted → string; `null`/`none` → None; `true`/`false` → bool; else int, then float, then a bareword string. | — |
| `format("…{ref}…")` | A text template composing refs with literal text (§8.4). | Placeholders must be valid ref calls. |

An unknown reference kind is a load-time error.

### 8.1 `preset(<token>)`

Reads a value from the matched `input_selector` entry or the `preset` header. The tokens
are exactly `PRESET_TOKENS`:

`path`, `name`, `format`, `table`, `sheet`, `sql`, `id`, `tier`, `os`, `tool`, `version`,
`os_version`.

The common use is `raw_source_path: preset(path)`, so a device-DB row records the
canonical device path the preset targets regardless of how the file was read.

### 8.2 Mapping form (field attributes)

| Attribute | Effect |
| --- | --- |
| `from` | The reference or conditional (**required** in mapping form). |
| `type` | Override the inferred cast: one of `int`, `float`, `str`, `bool`. |
| `unit` | Declare the **source** unit; the engine converts to the model's canonical unit (§10.2). **Required on every metric field** — use `unit: <canonical>` to confirm a value is already canonical. |
| `extract` | `pattern.group` — pull a **named** regex group of a named `patterns` entry. Selects verbatim source characters (no conversion). |
| `pipe` | A procedural call-chain (§10). |
| `optional` | When `true`, a mapped-but-absent field yields a null cell instead of disqualifying the preset match. |

Numeric model fields are cast automatically from their declared model type (`column_cast`
in `core/vocabularies.py`), so a plain `latitude: field(Lat)` needs no `type`. The `bool`
cast reads recognised spellings, **not** Python truthiness (§10.1).

The resolution tail for a **scalar** field is: ref/conditional → `extract` → `pipe` →
`unit` → `type`/inferred cast. Verbatim fields (the spatial parts, `entity`, `concerns`,
the two `time_accuracy_us_*`) apply only ref/conditional → `extract`; their pipe/unit/cast
is deliberately not run, because a component `raw_value` is Layer-1 evidence and every
conversion is a Layer-2 method's job.

### 8.3 Conditional values (`if / elif / else`)

Parsed by `expr.parse_cond`, evaluated by `resolve.resolve_cond`. A mapped value may be a
conditional chain over the same reference vocabulary. Branches are tested top-to-bottom
and the **first match wins**:

```yaml
spatial_temporal_link: if field(ExitTime) == "NA" then const(instant)
                       elif field(Confirmed) == "1" then const(continuous_during_interval)
                       else const(once_during_interval)
```

- Each test is `<ref> == <operand>` or `<ref> != <operand>`. Only `==` and `!=` are
  supported; any other operator is a load-time error.
- The operand is another ref (`field(A) != field(B)`) or a bare/quoted literal.
- The comparand `"NA"` (or bare `NA`) is the explicit **missing-value test**: it holds when
  the tested field is absent/empty, consulting `is_missing`, not when the cell literally
  reads `NA`.
- Equality tolerates source type drift: a CSV `"1"` and a numeric `1` match, and an
  **integral float** (`1.0`) matches its integer literal `"1"` (via `value_map_key`).
- A comparand that **looks like a call but names an unknown kind** (a typo — `feild(Y)`)
  **raises at parse time** rather than degrading to an unmatchable string literal. Quote a
  genuine literal (`"foo(bar)"`) to keep it a string.
- With **no `else` and no matching branch** the value is the **`N/A` sentinel** —
  deliberately distinct from an unmapped field: `N/A` means "the conditional ran and
  nothing matched"; an empty (NULL) cell means "never mapped". The sentinel stores/serialises
  as the text `N/A`.
- A `const(...)` branch **result** assigned to a controlled-vocabulary field is validated
  against that vocabulary at load time, exactly like a plain `const` (the test/comparand
  refs are not enum-checked — they are values to compare, not assigned).
- Conditionals work anywhere a field mapping does — `common`, `position`, time overrides,
  `zone`, `source_record_uid`. The short `links:` block stays bare-enum only; map a
  conditional link by its full column name. Conditional **time bounds** are not yet
  supported.
- In the mapping form, `from` may hold a conditional; the winning branch's value then flows
  through `type` / `unit` / `extract` / `pipe` as usual (the `N/A` sentinel short-circuits
  them — it is a terminal marker, not a value to transform).

### 8.4 Templates (`format(...)`)

Parsed by `expr.parse_template`, rendered by `resolve.resolve_template`.

```yaml
source_label: format("{field(FirstName)} {field(LastName)} from {field(City)}")
```

- Every `{…}` placeholder is one reference call from the vocabulary above; text outside the
  braces is verbatim. `{{` / `}}` write a literal brace.
- A missing/NA placeholder renders as **empty text** (the connecting literals stay); when
  **every** placeholder is missing the whole value is the **`N/A` sentinel** (consistent
  with conditionals — "evaluated, produced nothing", distinct from NULL).
- `format(...)` is an ordinary reference kind, so it composes with conditionals
  (`… then format(…)`) and with the `pipe` tail for free.
- A malformed template (unbalanced brace, empty `{}`, unknown ref kind, empty template) is
  a load-time error.

## 9. `links`

The observation's three edges, grouped, as bare enum tokens:

```yaml
links:
  entity_position:  at                 # -> entity_position_link
  entity_time:      observed_at        # -> entity_time_link
  spatial_temporal: instant            # -> spatial_temporal_link
```

Each key is optional; each value is validated against its controlled vocabulary at load
time (§14). Only these three keys are accepted; any other raises. For a value that must
come from a conditional or a reference, map the full column name (`entity_position_link`,
…) in `common:` or `position:` instead.

## 10. Pipes

Parsed by `expr.parse_pipe`, run by `pipes.run_pipe`. A pipe is a left-to-right,
`|`-separated call-chain string. Arguments are positional or `key=value`; scalar values are
coerced (int/float/bool/null/str), and an unquoted bareword stays a string. Each step is one
registered transform (`@register_transform`):

| Step | Signature | Meaning |
| --- | --- | --- |
| `cast` | `cast(int\|float\|str\|bool)` | Coerce the value. `bool` uses the recognised-spelling rule (§10.1). |
| `scale` | `scale(factor)` | Multiply: `float(value) * float(factor)`. |
| `arithmetic` | `arithmetic(<expr>)` | Evaluate a sandboxed expression with `value` bound (e.g. `arithmetic((value + 1) * 2)`). |
| `lookup` | `lookup(table, on_unknown=raw\|null\|error)` | Map through a named `lookup_tables` entry (§10.3). Default `on_unknown=raw`. |
| `regex` | `regex(pattern, group=<name>)` | Return a **named** capture group of a named `patterns` entry. The group is mandatory. |
| `split` | `split(sep, index=<n>)` | Split on `sep`; with `index` return that part, without it return the whole list. |

`None` is a no-op for every transform: an empty source cell flows through a pipe untouched
rather than raising.

### 10.1 The `on_error` policy

Every step accepts `on_error=null|raw|error` (default `null`), handled by `run_pipe`:

- `null` — on failure, the value becomes None (a warning is recorded).
- `raw` — on failure, the value passes through unchanged (a warning is recorded).
- `error` — on failure, the exception propagates and halts.

A `TransformHardError` (unknown transform, `on_unknown=error`, a lookup key collision, an
unsupported cast target) **always** propagates regardless of `on_error`.

### 10.2 `unit` conversion

`unit:` declares the **source** unit; the engine multiplies by `unit_factor(source_unit,
field)` to reach the field's canonical unit (`CANONICAL_UNIT` in `core/vocabularies.py`).
Recognised conversions (the six duration rows serve `duration_sec`, §11.1 — there the factor
is applied at L2, never to the stored L1 value):

| Source unit | Canonical | Factor |
| --- | --- | --- |
| `m/s` | km/h | 3.6 |
| `km/h` | km/h | 1.0 |
| `mph` | km/h | 1.609344 |
| `knots` | km/h | 1.852 |
| `m` | m | 1.0 |
| `ft` | m | 0.3048 |
| `deg` | deg | 1.0 |
| `s` | s | 1.0 |
| `ms` | s | 0.001 |
| `us` | s | 0.000001 |
| `min` | s | 60.0 |
| `h` | s | 3600.0 |
| `d` | s | 86400.0 |

An unknown unit/field pair raises at parse time. **`unit:` is REQUIRED on every metric field**
(any `CANONICAL_UNIT` key: `altitude_m`, the accuracy/speed/heading/beam parts). The loader
**raises** a `ValueError` if such a field is mapped with no `unit:`, because a bare mapping's
unit would otherwise be assumed canonical and a forgotten source unit would silently mis-scale
the value. To confirm a source is already canonical, state the canonical unit explicitly
(`unit: m` on `altitude_m`, `unit: deg` on `heading_deg`) — factor 1.0, but the intent is now
declared rather than assumed. (`latitude` / `longitude` are **not** metric — they are angles
adopted via `spatial_referential` — and need no `unit:`.)

### 10.3 `lookup` key normalisation

Lookup keys are normalised through `value_map_key`, so a table's `1:` and a source `"1"`
match. A table that declares **colliding** keys after normalisation (both `1:` and `"1":`)
**raises** a `TransformHardError` rather than letting one mapping silently win — a forensic
value-map must be unambiguous.

## 11. `time`

Parsed by `_parse_time`. **Required** on every observation.

```yaml
time:
  instant: field(TS)               # OR  interval: {lower: field(A), upper: field(B)}
  epoch:   cocoa                     # OR  format: "%d.%m.%Y %H:%M:%S.%f"  (or iso8601)
  zone:    const(UTC)               # const or header(...); applied during format parsing
```

| Key | Meaning |
| --- | --- |
| `instant` | A single time bound. **Exactly one of `instant` / `interval`.** |
| `interval` | `{lower: <bound>, upper: <bound>}` or `{lower: <bound>, duration_sec: {from:, unit:}}`. **Exactly one of `instant` / `interval`.** |
| `epoch` | A named epoch codec. **`epoch` and `format` are mutually exclusive.** |
| `format` | An strptime format string, or the marker `iso8601`. |
| `zone` | A `const(...)` or `header(...)` field-spec; a UTC offset, `local`, or an **IANA zone name**. |

### 11.1 `duration_sec` — the upper bound as an elapsed span

An `interval` needs `lower` plus **exactly one of** `upper` (an end clock) or `duration_sec`
(a span from the lower). The second is the CDR shape: operators disclose a start and a call
length, never a start and an end.

```yaml
time:
  interval:
    lower:        field(TimeStamp)
    duration_sec: { from: field(Duration), unit: s }
  format:              "%Y-%m-%d %H:%M:%S"
  zone:                const(Europe/Zurich)
  temporal_role_upper: const(end)
```

`duration_sec` names its **canonical** unit in the key, exactly like `altitude_m`, and the
**source** unit is declared with `unit:` — mandatory, as for every metric field. Admitted
source units: `s`, `ms`, `us`, `min`, `h`, `d`.

Layer 1 stays verbatim: the component holds the source's own digits (`"187"`) under the part
**`duration`** (instead of `upper`; an explicit `temporal_part_upper` still wins), and the
`unit:` never touches the stored value. Layer 2 derives the end — `upper = lower + duration` —
under the composite method `…+start_plus_duration/1.0`, whose leading fragment names the rule
that decoded the start. The derived end is exact in UTC and rendered in the offset the start
was stated in. An unreadable or negative span costs the end bound only: the row keeps its
start, and nothing is invented.

The flat CSV projects the verbatim span in `time_upper_raw` (the column coalesces the `upper`
and `duration` parts), with the derived end in `time_upper_unix_utc_us` / `time_upper_datetime_iso`.

### 11.2 `zone` — offsets and named zones

`zone:` accepts three spellings, all resolved per bound:

| Spelling | Behaviour |
| --- | --- |
| `const(+01:00)`, `const(UTC)` | The stated **fixed** offset, applied season-blind. |
| `const(Europe/Zurich)` | The source's own **IANA zone**: each bound resolves its own offset through the tz database, so a winter bound gets +01:00 and a summer bound +02:00. The tzdb version is stamped on the method. |
| `const(local)`, `…[DST]` | Device-local: resolved against the operator's configured `[timezone].local_zone`. |

A **named zone outranks the configured local zone** — the preset states what this source's
clocks mean, while the configuration only says what to assume when the source says nothing.

A `const(...)` zone that is neither an offset, nor `local`, nor a zone the tz database knows
is a **load-time error**: an unrecognised zone does not fail at conversion, it silently falls
back to assuming UTC.

- `epoch` ∈ `unix_s`, `unix_ms`, `unix_us`, `unix_ns`, `cocoa` (Apple 2001 epoch),
  `webkit`. (`unix_ns` has no `TemporalReferential` member yet and derives the referential
  `unknown`.)
- A **bound** is a bare reference, or `{from: <ref>, extract: pattern.group}` (the `extract`
  pulls a verbatim substring, e.g. a time out of a free-text statement).
- Any other temporal field named inside `time:` (accuracy, roles, parts, `temporal_source`,
  `temporal_referential`) is parsed as an **override** and applies to the bounds.
- **With neither `epoch` nor `format`**, the value is assumed to already be **Unix
  microseconds**.
- **With no `zone`**, the tool assumes **UTC** and marks the assumption loudly with a
  trailing ` N/A` on the materialised datetime (the `UTC_OFFSET_UNKNOWN` sentinel), so an
  examiner sees it was assumed, not stated.

The verbatim bound values become `temporal_component` rows (parts `lower` / `upper`, or a
preset-renamed part); the `zone` text is stored as each bound's `stated_offset`. The
canonical UTC µs and the offset-tagged local ISO strings are Layer-2 projections written by
the integration method, not by the preset.

## 12. Spatial datum rule

To integrate coordinates, a preset must do **two** things:

1. map the datum-agnostic `latitude` / `longitude` parts, **and**
2. declare the datum explicitly: `spatial_referential: const(epsg:4326)`.

The `const(...)` form is **required** — a bare `epsg:4326` fails the reference parser (it
is not a call form). The datum is never inferred from the presence of an angle part: when a
preset maps coordinates but names **no** referential, the spatial expression resolves to
`unknown`, the coordinates are **not** integrated, and a warning is surfaced. Nothing is
silently assumed to be WGS84.

## 13. Record identity

Three columns, three jobs:

| Column | Grain | Job | Mappable |
| --- | --- | --- | --- |
| `input_record_id` | one per source line | **Trace back** to the input record (defaults to the 1-based `<table-or-sheet>#<line>`, or a mapped tool locator). | yes (assignable field) |
| `source_record_uid` | one per source line | **Group** the output rows a record fans out into (shared by all of them). | yes (top-level key) |
| `row_uid` | one per output line | **Distinguish / count** every emitted output row (unique). | no (engine-set) |

- **`source_record_uid`** (optional top-level key, parsed as a non-assignable field-spec):
  reference a genuine source UID (a tool's Item ID, a real UUID column) when one exists; the
  value is used verbatim, stored on the observation, and projected into the CSV
  `source_record_uid` column in place of the generated id. Because it is preset-resolved it
  lives at **L1 on the observation**, beside `input_record_id` — never on the L0 `raw_record`,
  which is written before any preset is matched. It repeats on every observation one record
  fans out into: that repetition is the point, since the column's job is to **group** them.
  For a device DB write **`source_record_uid: default`** (or omit the key) so the engine
  generates a deterministic, content-addressed UID independent of the file name/path
  (folder == zip parity). The `default` token and omission behave identically; write the
  token, because omission and oversight are otherwise indistinguishable in the file, and the
  linter advises only on the presets that stayed silent. A bare rowid (`Z_PK`) is not a
  stable UID and should not be mapped here. A **mapped** duplicate `source_record_uid` across
  distinct source records is a hard error; the generated one never collides.
- **`row_uid`** is engine-only, a deterministic uuid5 over the output row's model data + the
  source line number + the output-row ordinal.

## 14. Controlled vocabularies

The `const` value of a controlled-vocabulary field (or a conditional's `const` result) must
be a legal member. **Validated at load time** (`_ENUM_FIELDS` in `spec.py`):
`entity_position_link`, `entity_time_link`, `spatial_temporal_link`, `source_tier`,
`deleted` (and the header `tier`). The other vocabularies below are typed columns whose
value is checked by the run-DB CHECK at write time — an illegal token flows as text and is
rejected **loudly** there (there is no silent drop). `entity_type`, `identifier_type`,
`position_type` (spatial role) and `spatial_referential` are free-text columns: an
unrecognised value is simply never normalised / never integrated.

The members (from `core/vocabularies.py`):

| Vocabulary | Field(s) | Members |
| --- | --- | --- |
| `EntityPositionLink` | `entity_position` / `entity_position_link` | `at`, `within_range_of`, `at_fixed_detector`, `references`, `claimed_at`, `inferred`, `unknown` |
| `EntityTimeLink` | `entity_time` / `entity_time_link` | `observed_at`, `event_at`, `recorded_at`, `reported_for`, `intended_for`, `never_during_interval`, `unknown` |
| `SpatialTemporalLink` | `spatial_temporal` / `spatial_temporal_link` | `instant`, `continuous_during_interval`, `once_during_interval`, `sporadic_during_interval`, `never_during_interval`, `unknown_during_interval` |
| `EntityRole` | `entity_role` | `subject`, `caller`, `callee`, `sender`, `recipient`, `reporter`, `owner`, `unknown` |
| `IdentifierType` | `identifier_type` | `msisdn`, `imei`, `imsi`, `email`, `plate`, `account`, `mac`, `ip`, `cell`, `camera`, `case_ref`, `other`, `none` (recognised kinds; the column is free text) |
| `EntityType` | `entity_type` | `device`, `person`, `vehicle`, `account`, `camera`, `antenna`, `case`, `label`, `event`, `poi`, `unknown` |
| `SpatialReferential` | `spatial_referential` | `epsg:4326`, `geohash`, `mgrs`, `address`, `place_name`, `cell_ref`, `asset_ref`, `region`, `unknown` |
| `SpatialRole` | `position_type` | `vicinity`, `entry`, `exit`, `origin`, `destination`, `waypoint`, `unknown` |
| `TemporalKind` | `time.instant` / `interval` (derived) | `instant`, `interval`, `open_lower`, `open_upper` |
| `TemporalReferential` | `temporal_referential` | `unix_epoch_s`, `unix_epoch_ms`, `unix_epoch_us`, `apple_cfabsolute`, `webkit`, `filetime`, `excel_serial`, `iso8601`, `local_string`, `unknown` |
| `TemporalRole` | `temporal_role_lower` / `_upper` | `start`, `end`, `booking`, `check_in`, `check_out`, `last_seen`, `unknown` |
| `ActivityKind` | `activity_kind` | `call`, `message`, `trip`, `transaction`, `stay`, `observation`, `unknown` |
| `SourceTier` | `tier` / `source_tier` | `primary`, `secondary`, `unknown` |
| `RecordState` | `deleted` | `live`, `intact`, `deleted`, `recovered`, `unknown` |

## 15. Load-time validation summary

The parser rejects, at load, a preset that:

- omits `preset.id` / `preset.name`, or `input_selector`, or a non-empty `observations`
  list;
- gives an `input_selector` neither/both of `path` and `name`, a `format` outside
  `csv|excel|sqlite|xml`, a sqlite selector neither/both of `table`/`sql`, an excel
  selector without `sheet`, or an xml selector without `model`;
- maps a field outside `ASSIGNABLE_FIELDS` (and not a `context.<key>`);
- uses a mapping form without `from`;
- references an unknown reference kind, an unknown `preset(...)` / `param(...)` /
  `filename(...)` token, or a `field(...)`/`header(...)` with no name;
- gives a `const` (or conditional `const` result) outside a load-validated vocabulary;
- has an `extract` that is not `pattern.group`, names an unknown pattern, or names a group
  the pattern does not define;
- writes a `patterns` entry that is not a valid regex;
- has a `time:` block with neither/both of `instant`/`interval`, both `epoch` and `format`,
  or an unknown `epoch`;
- writes a malformed conditional (bad operator, truncated chain, trailing tokens) or a
  malformed template.

A **runtime** (per-record) error — an unknown transform, `on_unknown=error` with no
mapping, a lookup key collision, an out-of-range unit — is raised by the pipe/resolver as a
`TransformHardError` or `ValueError` when the preset runs.
