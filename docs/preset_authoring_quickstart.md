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

## 2. Identify the source (`match`)

```yaml
match:                  # CSV
  type: csv
  as_file: "Cached Locations.csv"
  encoding: "utf-8-sig"
```

```yaml
match:                  # SQLite (direct file or inside a ZIP)
  type: sqlite
  in_archive: /private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite
  as_file: Cache.sqlite
  table: ZRTCLLOCATIONMO
```

Inside a ZIP the `in_archive` path selects the database; for a direct file the
`as_file` name does.

## 2b. Declare the columns you have (`expected_columns`)

List every source column up front, then map the subset you need. The unmapped ones
become the research frontier (reported in the warnings sidecar), and the inventory
drives drift detection and preset tie-breaking. Entries may be exact names or globs.

```yaml
expected_columns:
  - ZLATITUDE
  - ZLONGITUDE
  - ZTIMESTAMP
  - ZSPEED
  - "Timestamp Date/Time - * (dd.MM.yyyy)"   # a glob is fine
```

## 3. Reference values explicitly

Every value is one call: `column(...)`, `header(...)`, `filename(...)`,
`param(...)`, or `const(...)`.

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
      latitude_wgs84:  column(ZLATITUDE)      # numeric cast is inferred — no cast() needed
      longitude_wgs84: column(ZLONGITUDE)
      altitude_m:      column(ZALTITUDE)
      horizontal_speed_kmh: { from: column(ZSPEED), unit: m/s }   # converted to km/h
    time:
      instant: column(ZTIMESTAMP)
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
time: {instant: column(TS), format: "%d.%m.%Y %H:%M:%S.%f"}   # formatted string
time: {instant: column(TS), epoch: unix_ms}                   # epoch milliseconds
time: {instant: column(TS), epoch: cocoa, zone: const(UTC)}   # Apple Cocoa epoch
```

For a non-UTC export, capture the zone from the header and it is applied to parsing:

```yaml
patterns: {tz: "(?P<z>UTC[+-]\\d{2}:\\d{2})"}
...
    time:
      instant: 'column("Timestamp - * (dd.MM.yyyy)")'
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
  deleted: { from: 'column("Recovery method")', pipe: "lookup(recovery, on_unknown=null)" }
  # one cell holding "lat - lon": pull each named group into its own field
  latitude_wgs84:  { from: column(Coords), extract: coords.lat }
  longitude_wgs84: { from: column(Coords), extract: coords.lon }
```

`regex` requires named groups, and you name which group to extract — so what is pulled
out is explicit.

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
rowid mapped as `row_uid`), and ADVICE (naming, tier/tool coherence, naive-timezone,
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
