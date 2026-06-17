# Preset Authoring Quickstart

Use this guide when turning one real source export or database table into a
working mATLAS preset. Use [preset_schema.md](preset_schema.md) when you need
the full field reference.

## 1. Start From One Source

Pick one concrete source element first: one CSV file, one Excel sheet, or one
SQLite table/query. Keep the selector as narrow as possible while still matching
the source reliably.

CSV:

```yaml
selectors:
  - source_type: csv
    file_name: "Cached Locations.csv"
```

Excel:

```yaml
selectors:
  - source_type: excel
    file_name: "AXIOM Export.xlsx"
    sheet_name: "Cached Locations"
```

SQLite:

```yaml
selectors:
  - source_type: sqlite
    file_name: "Cache.sqlite"
    db_relpath: "/private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite"
```

For SQLite, the selector identifies the database. The table or SQL query belongs
under `extract.sqlite`.

## 2. Declare Extraction

Add only the extraction settings needed for that source type.

```yaml
extract:
  csv:
    delimiter: ","
    encoding: "utf-8-sig"
```

```yaml
extract:
  excel:
    sheet_name: "Cached Locations"
    header_row: 0
```

```yaml
extract:
  sqlite:
    table: "ZRTCLLOCATIONMO"
    # or:
    # sql: "SELECT * FROM ZRTCLLOCATIONMO"
```

## 3. Copy The Source Columns

List the columns you expect to see. This list is used for drift warnings; mapped
columns still come from `common`, `source_row_id`, and `assertions`.

```yaml
expected_columns:
  - "Item ID"
  - "Latitude"
  - "Longitude"
  - "Accuracy (m)"
  - "Timestamp Date/Time - UTC+00:00 (dd.MM.yyyy)"
```

Keep frontier columns in this list even if you do not map them yet. That makes
schema drift visible during later runs.

## 4. Add Identity And Common Fields

`source_row_id` should identify the source row stably. Use an exported item ID
when one exists. If omitted, mATLAS generates a deterministic ID from provenance
fields, but an explicit source ID is easier to audit.

```yaml
source_row_id:
  from: "Item ID"
```

`common` fields are copied into every assertion generated from a source row.

```yaml
source_tier: secondary

common:
  entity: {value: device}
  entity_type: {value: device}
  tool_label: {value: ZRTCLLOCATIONMO}
  position_source: {value: GNSS}
  record_locator: {from: "Location"}
  source_file_path: {from: "Source"}
```

### Entity And Linked Entity

`entity` and `linked_entity` can be set in the preset (usually in `common`) or
supplied at run time with `--entity` / `--linked-entity`. The run-level values are
**defaults**: they fill a row only when the preset did not already set that field,
so a preset mapping always wins. `--linked-entity` is required; `--entity` is
optional. Map them in the preset when they are intrinsic to the source (e.g.
`entity: {value: device}`); leave them to the run-level args when they are
case-specific (e.g. the subject's name).

### Field Mapping Sources

Every assignable field in `common`, `source_row_id`, an assertion, or a temporal
override draws its value from exactly **one** of four sources:

| Form | Value comes from | Use when |
| --- | --- | --- |
| `{from: "Latitude"}` | a source **column** value (name may be a glob) | the data is in a column |
| `{from_name: "Timestamp - *"}` | the matched **column name** itself | metadata is encoded in the header (e.g. a timezone) |
| `{from_file: name}` | the **source file** identity: `name`, `stem`, or `path` | you want the file name/path on the row, possibly combined with column values |
| `{value: device}` | a **constant** (a bare scalar works too) | the field is fixed for this preset |

`from_file` lets a preset use the filename *and* column values together — for
example record the source file name while still mapping coordinates from columns:

```yaml
common:
  record_locator: {from_file: name}   # e.g. "Cache.sqlite"
  tool_label: {from_file: stem}       # e.g. "Cache"
```

A glob column reference (`*`, `?`, `[`) must resolve to exactly one source column.

## 5. Map Assertions

Each item under `assertions` combines spatial/entity fields with one or more
temporal specs. One source row produces one output row for each temporal spec.

Single instant:

```yaml
assertions:
  - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
    longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
    horizontal_accuracy_m: {from: "Accuracy (m)", pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - instant: "Timestamp Date/Time - * (dd.MM.yyyy)"
        pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
        time_zone:
          from_name: "Timestamp Date/Time - * (dd.MM.yyyy)"
          pipe: [{regex_extract: "(UTC[+-][0-9]{2}:[0-9]{2})"}]
```

Interval:

```yaml
assertions:
  - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
    longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - interval:
          lower: "First Seen"
          upper: "Last Seen"
        pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
        entity_time_link: observed_at
        spatial_temporal_link: continuous_during_interval
```

Two instants at the same position:

```yaml
assertions:
  - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
    longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
    entity_position_link: at
    temporal:
      - instant: "Departure Time"
        pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
        entity_time_link: event_at
        spatial_temporal_link: instant
      - instant: "Arrival Time"
        pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
        entity_time_link: event_at
        spatial_temporal_link: instant
```

## 6. Use Pipes For Conversion

Common pipes:

- `cast`: convert to `int`, `float`, `str`, or `bool`.
- `parse_datetime`: parse a formatted timestamp to Unix nanoseconds.
- `arithmetic`: evaluate a restricted expression with `value` bound.
- `lookup`: map source codes to labels.
- `regex_extract`: extract one regex capture group.
- `split`: split a string and optionally pick one part.

Example source-code lookup:

```yaml
position_source:
  from: "Provider Code"
  pipe:
    - lookup:
        1: GNSS
        4: WiFi
        6: LTE
      on_unknown: raw
```

Example speed conversion:

```yaml
horizontal_speed_kmh:
  from: "Speed (m/s)"
  pipe:
    - {cast: float}
    - {arithmetic: "value * 3.6"}
```

### Timestamp Recipes

A temporal spec writes its pipe result to `time_*_unix_ns`, so any pipe chain that
ends in Unix nanoseconds works — `parse_datetime` is just the common case.

Formatted string (most AXIOM/CSV exports):

```yaml
pipe: [{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}]
```

Unix epoch **seconds** (numeric column):

```yaml
pipe: [{cast: float}, {arithmetic: "int(value * 1000000000)"}]
```

Unix epoch **milliseconds**:

```yaml
pipe: [{cast: float}, {arithmetic: "int(value * 1000000)"}]
```

Cocoa / Core Data timestamp (seconds since 2001-01-01 UTC — Apple `ZTIMESTAMP`,
`ZDATE`, etc.). Add the 978307200-second offset between the Unix and Cocoa epochs:

```yaml
pipe: [{cast: float}, {arithmetic: "int((value + 978307200) * 1000000000)"}]
```

See `presets/ios/ios_routined_cached_locations.yaml` for the Cocoa recipe in a
complete SQLite preset.

## 7. Validate Before Full Runs

Run one small source first. `--linked-entity` is required (the case subject every
row is attributed to); `--entity` is optional:

```bash
python matlas.py process \
  --input ./evidence/sample \
  --presets ./presets \
  --output ./out/merged.csv \
  --linked-entity "Case Subject"
```

Inspect:

- `merged.csv`
- `merged.matlas.traceability.json`
- `merged.matlas.warnings.json`

To test just your new preset against just one file, point `--input` at that file
and `--presets` at the single YAML. mATLAS then runs in **force-preset mode**: it
applies the preset to the file and skips selector matching entirely, so you can
iterate even before the selector is finalised:

```bash
python matlas.py process \
  --input ./evidence/Cache.sqlite \
  --presets ./presets/ios/ios_routined_cached_locations.yaml \
  --output ./out/routined.csv \
  --linked-entity "Case Subject"
```

Use split output when comparing preset behavior independently:

```bash
python matlas.py process \
  --input ./evidence/sample \
  --presets ./presets \
  --output ./out/by-preset \
  --no-merge \
  --linked-entity "Case Subject"
```

## 8. Use Profiles For Curated Preset Sets

The GUI can save selected presets into a `.mATLAS-profile` file. The CLI can
then run the same selection:

```bash
python matlas.py process \
  --input ./evidence \
  --profile ./profiles/ios-locations.mATLAS-profile \
  --output ./out/merged.csv \
  --linked-entity "Case Subject"
```

Profile files are JSON:

```json
{
  "version": 1,
  "preset_paths": [
    "../presets/axiom/ios_cached_locations.yaml",
    "../presets/axiom/ios_significant_locations_visits.yaml"
  ]
}
```

Relative profile paths are resolved from the profile file location. The GUI
saves absolute paths for convenience.
