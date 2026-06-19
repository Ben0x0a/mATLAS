# Preset YAML Schema (v3)

Presets are YAML files loaded recursively from a preset folder, or loaded directly
when `--presets` points to one YAML file. A preset selects one source, extracts rows,
and maps each source row into one or more canonical assertion rows.

The v3 format is examiner-first: types are inferred, timestamps use named codecs, the
assertion's relationship is grouped, and every value is an explicit reference call.

## Shape

```yaml
preset:
  id: ios.routined.cached_locations     # stable machine key (traceability)
  name: Routined Cached Locations        # human name
  os: iOS                                # composes the title
  tool:                                  # forensic tool; empty for a primary source
  os_version: ">=15"                     # applicability range (tie-break)
  version: 1.0
  tier: primary                          # primary | secondary | unknown

match:
  type: sqlite                           # csv | excel | sqlite
  in_archive: /private/var/.../Cache.sqlite   # internal path (ZIP source)
  as_file: Cache.sqlite                  # file name (direct source)
  table: ZRTCLLOCATIONMO                 # sqlite table (or sql:)

expected_columns:                        # the source's full column inventory (globs ok)
  - ZLATITUDE
  - ZLONGITUDE
  - ZTIMESTAMP
  - ZSPEED

record_uid: column(Z_PK)                 # optional; omit to auto-generate a deterministic UID

lookup_tables:                           # named tables for lookup() pipe steps
  recovery: {Parsing: intact, Carving: recovered}
patterns:                                # named regex with mandatory named groups
  coords: "(?P<lat>-?\\d+\\.\\d+) - (?P<lon>-?\\d+\\.\\d+)"

common:                                  # fields shared by every assertion of a row
  entity: const(device)

assertions:                              # each entry = one position + one time + links
  - position:
      latitude_wgs84:  column(ZLATITUDE)
      longitude_wgs84: column(ZLONGITUDE)
      horizontal_speed_kmh: { from: column(ZSPEED), unit: m/s }
    time:
      instant: column(ZTIMESTAMP)
      epoch:   cocoa
      zone:    const(UTC)
    links:
      entity_position:  at
      entity_time:      observed_at
      spatial_temporal: instant
```

## `preset` header

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable dotted identifier, recorded in traceability. |
| `name` | yes | Short human name. |
| `os` | no | Platform; composes the title. |
| `tool` | no | Forensic tool (AXIOM, Cellebrite). Empty for a primary/device source. |
| `os_version` | no | Comparator range (`">=15"`, `">=15 <18"`); empty = any version. |
| `version` | no | Preset version. |
| `tier` | no | `primary` (parsed device artefact) or `secondary` (a tool's export). |

The title is composed: the non-empty of `os`/`tool` joined by a space, then `— name`
(e.g. `iOS — Routined Cached Locations`, or `iOS AXIOM — Cached Locations`).

## `match`

Identifies the source and how to read it.

| Field | Applies to | Meaning |
| --- | --- | --- |
| `type` | all | `csv` \| `excel` \| `sqlite`. |
| `as_file` | all | File name (matches a direct file/folder source). |
| `in_archive` | sqlite | Internal acquisition path (matches a DB inside a ZIP). |
| `sheet` | excel | Sheet name. |
| `table` / `sql` | sqlite | Exactly one: a table name or a read-only SQL query. |
| `delimiter`,`encoding`,`header_row`,`skip_rows` | csv/excel | Read options. |

**Matching is context-aware:** inside a ZIP the `in_archive` path discriminates; for a
direct file the `as_file` name does. When several presets match one source, the engine
tie-breaks by **structural fit** — how well each preset's `expected_columns` match the
source's actual columns (falling back to the mapped columns when no inventory is
declared) — then declaration order.

## `expected_columns`

The full source-column inventory the examiner declares **before** mapping — the
intended authoring flow is "list every column you have, then map the subset you need".
It powers the drift / frontier report (present-but-unmapped = the research backlog;
declared-but-absent = drift) and the AXIOM differential.

```yaml
expected_columns:
  - Latitude
  - Longitude
  - "Speed (m/s)"
  - "Timestamp Date/Time - * (dd.MM.yyyy)"   # entries may be exact names OR globs
```

Entries may be exact names or glob patterns (a glob matches any present column). It is
optional but recommended; the linter advises when it is missing and warns when the
mapping reads a column the inventory does not cover.

## Reference vocabulary (field values)

Every mapped value is exactly one explicit call:

| Form | Value |
| --- | --- |
| `column(NAME)` | a source column's value (NAME may be a glob; quote if it has spaces) |
| `header("Glob *")` | the matched column's header text (e.g. a timezone in the header) |
| `filename(name\|stem\|path)` | part of the source file identity |
| `param(entity\|linked_entity)` | a run-level argument |
| `preset(in_archive\|table\|id\|...)` | a key from the current preset's own `match`/`meta` |
| `const(VALUE)` | a literal |

A field may instead be a mapping with attributes:

```yaml
horizontal_speed_kmh: { from: column(ZSPEED), unit: m/s }
imei:                  { from: filename(name), pipe: "regex(imei, group=n)" }
latitude_wgs84:        { from: column(Coords), extract: coords.lat }
```

| Attribute | Effect |
| --- | --- |
| `from` | the reference (required in mapping form) |
| `type` | override the inferred cast (`int`/`float`/`str`/`bool`) |
| `unit` | declare the source unit; the engine converts to the model's canonical unit |
| `extract` | `pattern.group` — pull a named regex group from a named pattern |
| `pipe` | a procedural call-chain (see Pipes) |

Numeric model columns are **cast automatically** from their declared type, so a plain
`latitude_wgs84: column(Lat)` needs no `cast`.

Bare scalars are allowed only where the position is a known keyword/enum — link values,
`epoch`, `zone` — never where a column could be meant.

## `time`

```yaml
time:
  instant: column(TS)               # or  interval: {lower: column(A), upper: column(B)}
  epoch:   cocoa                     # OR  format: "%d.%m.%Y %H:%M:%S.%f"
  zone:    const(UTC)               # const or header(...); applied to format parsing
```

- Exactly one of `instant` / `interval`.
- Exactly one decoding: `epoch` (named) or `format` (strptime). With neither, the value
  is assumed to already be Unix microseconds.
- `epoch` ∈ `unix_s`, `unix_ms`, `unix_us`, `unix_ns`, `cocoa` (2001 epoch), `webkit`.
- A captured `zone` (e.g. `header(...)` + `regex`) is applied during `format` parsing.
- The raw value, the resolved source column, and the normalized `*_unix_us` are written
  automatically to the `time_lower_*` / `time_upper_*` columns.

## `links`

The assertion's three edges, grouped:

```yaml
links:
  entity_position:  at                 # -> entity_position_link
  entity_time:      observed_at        # -> entity_time_link
  spatial_temporal: instant            # -> spatial_temporal_link
```

Each is validated against its controlled vocabulary.

## Pipes (the procedural escape hatch)

Most fields need no pipe — type, unit, epoch, and extract are declarative. When you do
need logic, a pipe is a left-to-right call chain string:

```yaml
deleted: { from: 'column("Recovery method")', pipe: "lookup(recovery, on_unknown=null)" }
heading_deg: { from: column(Raw), pipe: "split(';', index=0) | cast(float)" }
```

| Step | Example | Meaning |
| --- | --- | --- |
| `cast` | `cast(int)` | coerce to int/float/str/bool |
| `scale` | `scale(3.6)` | multiply by a factor |
| `arithmetic` | `arithmetic((value + 1) * 2)` | sandboxed expression with `value` bound |
| `lookup` | `lookup(recovery, on_unknown=null)` | map via a named `lookup_tables` entry |
| `regex` | `regex(coords, group=lat)` | a **named** capture group of a named `patterns` entry |
| `split` | `split(',', index=0)` | split and optionally pick one part |

`regex` requires named groups; the author names which group to extract. Each step
accepts `on_error=null|raw|error` (default `null`).

## `record_uid`

Optional. Reference a genuine source UID (a tool's Item ID, a real UUID column) when one
exists; the value is used verbatim so output rows link back to the tool artefact. Omit it
for a device DB so the engine generates a deterministic, content-addressed UID
(`uuid5(content_fingerprint | raw_source_path | input_file | input_record_id)`). A bare
rowid (`Z_PK`) is not a stable UID and should not be mapped here.

## `preset(...)` reference

`preset(<key>)` reads a value from the current preset's own definition — its `match`
block (`in_archive`, `as_file`, `table`, `sheet`, `sql`) or `meta` (`id`, `name`, `tier`,
`os`, `tool`, `version`, `os_version`). The common use is `raw_source_path:
preset(in_archive)`, so a device-DB row records the canonical device path the preset
targets regardless of how the file was read.

## Provenance to map

- `raw_source_path` — where the trace came from. Map it explicitly:
  `preset(in_archive)` for a device DB, `column(Source)` for a tool export.
- `input_record_id` — which record in the file; defaults to `<table-or-sheet>#<ordinal>`
  when unmapped, or map a tool locator column.
- `source_label`, `deleted` — optional descriptive label and record state.
- `input_file`, `preset_id`, `preset_name` are engine-set and must not be mapped.

## Validation notes

- `assertions` must be non-empty; each needs a `time`.
- A field value must be an explicit reference call; a missing column resolves to `None`.
- A glob must resolve to exactly one source column.
- A duplicate `record_uid` (generated or mapped) across distinct rows is a hard error.
- Engine-owned columns (the `time_*`, `latitude_source_field`, `longitude_source_field`,
  `input_file`, `record_uid`, `preset_id`, `preset_name`, `record_*`) are not assignable
  in a mapping.
