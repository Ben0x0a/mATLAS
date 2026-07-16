# Preset Authoring Quickstart (v3)

Use this guide to turn one real source — a CSV/Excel export or a SQLite table — into a
working mATLAS preset. See [preset_schema.md](preset_schema.md) for the full reference.

## 1. Write the header

`id` is a stable dotted key; `tool` is the forensic tool for a tool export and empty
for a raw device source; together with `os`/`name` it composes the title.

```yaml
preset:
  id: ios.routined.cached_locations
  name: Routined Cached Locations
  os: iOS
  tool:                 # empty -> primary (device's own DB); set "AXIOM" etc. for exports
  os_version: ">=15"
  version: 1.0
  tier: primary
```

## 2. Identify the source (`input_selector`)

```yaml
input_selector:         # CSV — basename match anywhere (a tool export)
  format: csv
  name: "Cached Locations.csv"
  encoding: "utf-8-sig"
```

```yaml
input_selector:         # SQLite — anchored device path (folder or ZIP, prefix-tolerant)
  format: sqlite
  path: /private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite
  table: ZRTCLLOCATIONMO
```

Use `name:` for the easy basename case (it fans out to every matching file). Use `path:`
when location is the discriminator — it is an anchored, prefix-tolerant path that matches
a loose folder tree and a full-filesystem ZIP alike (skipping a `filesystem1/` wrapper up
to `--root-prefix-depth`, default 1), with `{uuid}` and `*` for variable segments. Pass a
list of selectors for OR alternatives. `format` is always magic-verified.

## 2b. Declare the fields you have (`expected_fields`)

List every source field up front, then map the subset you need. The unmapped ones
become the research frontier (reported in the warnings sidecar), and the inventory
drives drift detection and preset tie-breaking. Entries may be exact names or globs.

```yaml
expected_fields:
  - ZLATITUDE
  - ZLONGITUDE
  - ZTIMESTAMP
  - ZSPEED
  - "Timestamp Date/Time - * (dd.MM.yyyy)"   # a glob is fine
```

## 3. Reference values explicitly

Every value is one call: `field(...)`, `header(...)`, `filename(...)`,
`param(...)`, `const(...)`, or `format(...)` (a text template, see 6c). A value may
also be an `if / elif / else` conditional over those calls (see 6b).

```yaml
common:
  entity:         const(device)
  tool_label:     const(ZRTCLLOCATIONMO)
  record_locator: filename(name)
  linked_entity:  param(linked_entity)   # or pass --linked-entity at run time
```

## 4. Map an assertion (`position` + `time` + `links`)

```yaml
assertions:
  - position:
      latitude_wgs84:  field(ZLATITUDE)      # numeric cast is inferred — no cast() needed
      longitude_wgs84: field(ZLONGITUDE)
      altitude_m:      field(ZALTITUDE)
      horizontal_speed_kmh: { from: field(ZSPEED), unit: m/s }   # converted to km/h
    time:
      instant: field(ZTIMESTAMP)
      epoch:   cocoa
      zone:    const(UTC)
    links:
      entity_position:  at
      entity_time:      observed_at
      spatial_temporal: instant
```

One assertion = one position + one time + the three links. For two times at one place
(e.g. a visit interval AND a created instant), write two assertion entries; they share
the row's UID automatically.

## 5. Timestamps

A `time` block decodes to Unix microseconds. Pick one:

```yaml
time: {instant: field(TS), format: "%d.%m.%Y %H:%M:%S.%f"}   # formatted string
time: {instant: field(TS), epoch: unix_ms}                   # epoch milliseconds
time: {instant: field(TS), epoch: cocoa, zone: const(UTC)}   # Apple Cocoa epoch
```

For a non-UTC export, capture the zone from the header and it is applied to parsing:

```yaml
patterns: {tz: "(?P<z>UTC[+-]\\d{2}:\\d{2})"}
...
    time:
      instant: 'field("Timestamp - * (dd.MM.yyyy)")'
      format:  "%d.%m.%Y %H:%M:%S.%f"
      zone:    { from: 'header("Timestamp - * (dd.MM.yyyy)")', pipe: "regex(tz, group=z)" }
```

## 6. Pipes and named tables/patterns (only when needed)

Declarative attributes cover most cases; drop to a pipe for real logic. Bulk data lives
in `lookup_tables` / `patterns` and is referenced by name:

```yaml
lookup_tables: {recovery: {Parsing: intact, Carving: recovered, Deleted: deleted}}
patterns:      {coords: "(?P<lat>-?\\d+\\.\\d+) - (?P<lon>-?\\d+\\.\\d+)"}
...
  deleted: { from: 'field("Recovery method")', pipe: "lookup(recovery, on_unknown=null)" }
  # one cell holding "lat - lon": pull each named group into its own field
  latitude_wgs84:  { from: field(Coords), extract: coords.lat }
  longitude_wgs84: { from: field(Coords), extract: coords.lon }
```

`regex` requires named groups, and you name which group to extract — so what is pulled
out is explicit.

## 6b. Conditional values (`if / elif / else`)

When a field's meaning depends on another field, map it as a conditional chain —
branches evaluate top-to-bottom, first match wins:

```yaml
common:
  spatial_temporal_link: if field(ExitTime) == "NA" then const(instant)
                         elif field(Confirmed) == "1" then const(continuous_during_interval)
                         else const(once_during_interval)
```

The comparand `"NA"` is the explicit *missing-value* test (the field is absent/empty).
`==` and `!=` are supported; a comparand may also be another ref (`field(A) != field(B)`).
With no `else` and no match the cell holds **`N/A`** — meaning "the conditional ran,
nothing matched" — which is deliberately different from an empty cell (the field was
never mapped). Conditional time *bounds* are not yet supported.

## 6c. Composed text (`format(...)`)

To build one value out of several source fields plus literal text, use a `format(...)`
template — each `{…}` placeholder is an ordinary reference call:

```yaml
common:
  source_label: format("{field(FirstName)} {field(LastName)} from {field(City)}")
```

A missing placeholder renders as empty text (the surrounding literals stay); if *every*
placeholder is missing the whole cell holds `N/A`, as for conditionals. `{{` / `}}`
write a literal brace. A template also works as a conditional branch result
(`… then format(…)`).

## 7. Run it

`--linked-entity` is required; `--entity` is optional. A single input file (one element)
plus a single preset YAML runs in **force-preset mode** (selector matching bypassed),
which is handy while iterating:

```bash
python matlas.py process \
  --input ./evidence/Cache.sqlite \
  --presets presets/ios/ios_routined_cached_locations.yaml \
  --output ./out/routined.csv \
  --linked-entity "Case Subject"
```

Inspect `out/routined.csv` plus the `.matlas.traceability.json` and
`.matlas.warnings.json` sidecars; the warnings sidecar lists unmapped columns (the
research frontier).

## 8. Lint your preset

Before relying on a preset, lint it for errors, likely mistakes, and best-practice
nudges:

```bash
python utils/lint_presets.py presets/ios/ios_routined_cached_locations.yaml --advice
```

It reports ERRORs (won't load), WARNINGs (e.g. a half-coordinate, a missing link, a
rowid mapped as `source_record_uid`), and ADVICE (naming, tier/tool coherence, naive-timezone,
raw epoch arithmetic). The same checks are available as a library:

```python
from model_atlas.presets import lint_file, lint_paths   # -> list[LintFinding]
```

`--strict` makes warnings fail the run (useful in CI); the default exit code is
non-zero only on errors.

## 9. Different OS versions

If a later OS renames columns, copy the preset to a new file, set its `os_version`
range, and adjust the changed column names. The engine auto-selects the preset whose
mapped columns actually fit the source — you don't choose by hand.
