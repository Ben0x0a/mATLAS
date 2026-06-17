# Preset YAML Schema

Presets are YAML files loaded recursively from a preset folder, or loaded
directly when `--presets` points to one YAML file. Template placeholders are
skipped. A preset selects one source element, extracts rows, and maps each
source row into one or more canonical assertion rows.

The current preset schema is v2 and uses:

- `common` for row-level fields shared by every assertion.
- `assertions` for spatial/entity fields plus temporal specs.
- `pipe` lists for type conversion, parsing, lookup, and other transformations.

## Minimal Shape

```yaml
name: Example Locations
version: "1.0"
parser:
  name: example_locations
  version: "1.0"

source_tier: secondary

selectors:
  - source_type: csv
    file_name: "locations.csv"

extract:
  csv:
    delimiter: ","
    encoding: "utf-8"

expected_columns:
  - "Item ID"
  - "Latitude"
  - "Longitude"
  - "Recorded At"

source_row_id:
  from: "Item ID"

common:
  entity: {value: device}
  entity_type: {value: device}
  tool_label: {value: locations.csv}

assertions:
  - latitude_wgs84: {from: Latitude, pipe: [{cast: float}]}
    longitude_wgs84: {from: Longitude, pipe: [{cast: float}]}
    position_source: {value: GNSS}
    entity_position_link: at
    temporal:
      - instant: "Recorded At"
        pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
        entity_time_link: observed_at
        spatial_temporal_link: instant
```

## Top-Level Fields

| Field | Required | Description |
| --- | --- | --- |
| `name` | yes | Human-readable preset name. Used in logs and split-output filenames. |
| `version` | no | Preset version. Defaults to `parser.version` when absent. |
| `parser.name` | yes | Stable parser identifier recorded in traceability. |
| `parser.version` | yes | Parser/preset version recorded in traceability. |
| `source_tier` | no | Source tier written into output when not otherwise mapped. |
| `selectors` | yes | One or more source-matching rules. |
| `extract` | no | Source-type extraction settings. |
| `expected_columns` | no | Drift warning inventory. |
| `source_row_id` | no | Explicit stable row ID. |
| `common` | no | Output fields shared by every assertion from a source row. |
| `assertions` | yes | Assertion templates; each must include `temporal`. |

## Selectors

CSV:

```yaml
selectors:
  - source_type: csv
    file_name: "locations.csv"
```

Excel:

```yaml
selectors:
  - source_type: excel
    file_name: "export.xlsx"
    sheet_name: "Locations"
```

SQLite:

```yaml
selectors:
  - source_type: sqlite
    file_name: "Cache.sqlite"
    db_relpath: "/private/var/mobile/Library/Caches/example/Cache.sqlite"
```

SQLite selectors identify the database. The table or SQL query is set under
`extract.sqlite`.

A selector matches when **any** criterion it declares matches (OR semantics), not
when all do. So a SQLite selector that gives both `file_name` and `db_relpath`
matches a source that matches *either* — useful when an acquisition renames the
file but preserves the internal path, or vice versa. A selector that declares no
criteria matches on source type alone.

## Extraction

```yaml
extract:
  csv:
    delimiter: ","
    encoding: "utf-8-sig"
    header_row: 0
    skip_rows: 0

  excel:
    sheet_name: "Locations"
    header_row: 0
    skip_rows: 0

  sqlite:
    table: "ZLOCATION"
    # exactly one of table or sql:
    # sql: "SELECT * FROM ZLOCATION"
```

## Field Mapping Syntax

Fields in `source_row_id`, `common`, assertions, and temporal overrides set
**exactly one** of `from`, `from_name`, `from_file`, or `value`:

```yaml
entity: {value: device}          # constant
latitude_wgs84: {from: Latitude} # source column value
time_zone:
  from_name: "Timestamp - *"     # resolved source column name
  pipe: [{regex_extract: "(UTC[+-][0-9]{2}:[0-9]{2})"}]
record_locator: {from_file: name}  # source file identity: name | stem | path
```

| Form | Value |
| --- | --- |
| `from` | the value of a source column (name may be a glob) |
| `from_name` | the matched column's *name* (e.g. to extract embedded metadata) |
| `from_file` | the source file identity — `name`, `stem`, or `path` |
| `value` | a constant |

A bare scalar is also a constant:

```yaml
entity_position_link: at
```

Column names may use a glob pattern such as `Timestamp Date/Time - *`. A glob
must resolve to exactly one source column.

## Assertions And Temporal Specs

Each `assertions` entry contains assignable output fields plus `temporal`.
`temporal` may be one mapping or a list of mappings.

Instant:

```yaml
temporal:
  - instant: "Recorded At"
    pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
    entity_time_link: observed_at
    spatial_temporal_link: instant
```

Interval:

```yaml
temporal:
  - interval:
      lower: "First Seen"
      upper: "Last Seen"
    pipe: [{parse_datetime: "%Y-%m-%d %H:%M:%S"}]
    entity_time_link: observed_at
    spatial_temporal_link: continuous_during_interval
```

Temporal raw values and source column names are captured automatically into:

- `time_lower_raw`
- `time_lower_source_field`
- `time_upper_raw`
- `time_upper_source_field`

The temporal pipe result is written to `time_lower_unix_ns` and
`time_upper_unix_ns`.

## Pipes

Pipes run in order and treat `None` as a no-op.

| Pipe | Example | Description |
| --- | --- | --- |
| `cast` | `{cast: float}` | Convert to `int`, `float`, `str`, or `bool`. |
| `parse_datetime` | `{parse_datetime: "%d.%m.%Y %H:%M:%S.%f"}` | Parse with `datetime.strptime` and write Unix nanoseconds. |
| `arithmetic` | `{arithmetic: "value * 3.6"}` | Restricted expression with `value` bound. |
| `lookup` | `{lookup: {1: GNSS, 4: WiFi}, on_unknown: raw}` | Map encoded values. |
| `regex_extract` | `{regex_extract: "(UTC[+-][0-9]{2}:[0-9]{2})"}` | Return one regex group. |
| `split` | `{split: ",", index: 0}` | Split a string and optionally select one part. |

## Assignable Output Fields

The preset can assign any canonical output field except engine-owned fields.

Engine-owned fields:

- `time_lower_raw`
- `time_lower_source_field`
- `time_lower_unix_ns`
- `time_upper_raw`
- `time_upper_source_field`
- `time_upper_unix_ns`
- `source_row_id`
- `record_type`
- `record_rank`

Common frequently mapped fields:

- `entity`, `entity_type`, `linked_entity`
- `time_zone`, `time_accuracy_ns`, `temporal_source`
- `latitude_wgs84`, `longitude_wgs84`, `altitude_m`
- `position_type`, `raw_position`, `position_source`
- `horizontal_accuracy_m`, `vertical_accuracy_m`
- `horizontal_speed_kmh`, `vertical_speed_kmh`
- `heading_deg`, `heading_accuracy_deg`
- `entity_position_link`, `entity_time_link`, `spatial_temporal_link`
- `source_file_path`, `tool_label`, `input_file`, `record_locator`
- `source_tier`, `deleted`

Some fields validate constants against controlled vocabularies, including
`source_tier`, `deleted`, and the three link fields.

## Validation Notes

- `selectors` and `assertions` must be non-empty.
- `expected_columns` warns on drift but does not control extraction.
- Missing mapped columns resolve to `None` unless a later pipe raises.
- Duplicate explicit `source_row_id` values across different source rows are a
  hard error.
- Preset logic does not live in the GUI; both CLI and GUI call the same package
  pipeline.
