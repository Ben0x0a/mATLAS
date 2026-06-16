# Preset YAML Schema

Presets are YAML files loaded recursively from `presets/**/*.yaml`. Presets
describe how a source is selected, extracted, and mapped into the canonical
integration model documented in [integration_model.md](integration_model.md).

The model documentation is intentionally separate from this YAML schema. The
model describes output columns; this document describes how presets populate
those columns.

## Full Commented Example

```yaml
# Human-readable preset name.
# Type: string. Unit: none. Example: "Example Device Locations".
name: "Example Device Locations"

# Preset version.
# Type: string. Unit: none. Example: "1.0".
version: "1.0"

# Parser identity recorded in traceability.
# Type: mapping. Unit: none.
parser:
  # Stable parser identifier.
  # Type: string. Example: "example_device_locations".
  name: "example_device_locations"

  # Parser/preset version recorded in output traceability.
  # Type: string. Example: "1.0".
  version: "1.0"

# One or more selectors. A source element matches a preset when one selector
# matches the source type and the optional filename/path/sheet constraints.
# Type: list of mappings.
selectors:
  # CSV selector.
  - source_type: csv
    file_name: "locations.csv"

  # Excel selector. file_name limits the workbook; sheet_name limits the sheet.
  - source_type: excel
    file_name: "report.xlsx"
    sheet_name: "Locations"

  # SQLite selector. db_relpath is the original path inside an evidence archive
  # or the discovered internal path for SQLite inside ZIP.
  - source_type: sqlite
    file_name: "Cache.sqlite"
    db_relpath: "/private/var/mobile/Library/Caches/example/Cache.sqlite"

# Extraction settings by format.
# Type: mapping.
extract:
  csv:
    # CSV delimiter. Type: string. Example: ",".
    delimiter: ","
    # Text encoding. Type: string. Example: "utf-8".
    encoding: "utf-8"
    # Header row index. Type: integer. Unit: zero-based row index.
    header_row: 0
    # Rows skipped before the header/data. Type: integer. Unit: rows.
    skip_rows: 0

  excel:
    # Sheet to read. Type: string. Unit: none.
    sheet_name: "Locations"
    # Header row index. Type: integer. Unit: zero-based row index.
    header_row: 0
    # Rows skipped before the header/data. Type: integer. Unit: rows.
    skip_rows: 0

  sqlite:
    # Table to extract. Type: string. Unit: none.
    # Exactly one of table or sql must be set.
    table: "ZLOCATION"
    # sql: "SELECT * FROM ZLOCATION"

# Expected source columns. Used to warn on schema drift.
# Type: list of strings. Unit: source column names.
expected_columns:
  - "DEVICE_ID"
  - "FIRST_SEEN"
  - "LAST_SEEN"
  - "RECORDED_AT"
  - "LATITUDE"
  - "LONGITUDE"
  - "HORIZONTAL_ACCURACY"
  - "POSITION_SOURCE"
  - "TEMPORAL_SOURCE"
  - "RAW_POSITION"
  - "RAW_TIMESTAMP"

# Timestamp normalization expression.
# Type: mapping. Unit: expression returns Unix milliseconds.
timestamp:
  unix_ms:
    # The expression sees one variable: value.
    # Type: string. Example below converts source seconds to Unix ms.
    expression: "int(value * 1000)"

# Tool label rules by source type.
# Type: mapping. Unit: none.
tool_label:
  csv:
    # Allowed values for from:
    # - file_name
    # - sheet_name
    # - table_name
    # - db_relpath
    # - source_path
    # - column
    # - value
    from: "file_name"
  excel:
    from: "sheet_name"
  sqlite:
    from: "table_name"

# Optional raw-data reference copied into Source raw data.
# Type: mapping. Unit: source-defined.
source_raw_data:
  # Use source_column for a source column, or source_value for a constant.
  source_column: null
  # source_value: "/original/raw/path"

# Common row-level mappings. These values are copied into every assertion
# generated from a source row.
# Type: list of mappings.
model_mapping:
  # source_column reads a source column.
  - model_name: "Entity"
    source_column: "DEVICE_ID"

  # source_value writes a constant.
  - model_name: "Entity type"
    source_value: "device"

  # source_column: null explicitly leaves the model field empty.
  - model_name: "Linked Entity"
    source_column: null

# Location mappings. Each item generates one or more spatio-temporal
# assertions from each source row.
# Type: list of mappings.
location_mappings:
  # A list under timestamp creates one instant assertion per timestamp column.
  - timestamp:
      - "col:FIRST_SEEN"
      - "col:RECORDED_AT"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:instant"
    Timestamp accuracy: null
    # Raw temporal expression before parsing/normalization, e.g.
    # "tomorrow", "next Monday", or a source-specific timestamp string.
    raw_timestamp: "col:RAW_TIMESTAMP"
    # Source/mechanism for this timestamp, e.g. value:NTP, value:internal_clock,
    # or col:TEMPORAL_SOURCE.
    temporal_source: "col:TEMPORAL_SOURCE"
    Altitude: null
    # Raw spatial expression before geocoding/inference, e.g. an address,
    # "I'm at home", a WiFi BSSID, or a cell identifier.
    raw_position: "col:RAW_POSITION"
    # Source/mechanism for this position. For encoded source values, use
    # source_column + labels. The original source value is kept in details by
    # default under value_map_references.
    position_source:
      source_column: "POSITION_SOURCE"
      labels:
        1: "GNSS"
        4: "WiFi"
        6: "LTE"
      # Unknown source codes are copied through as raw values by default.
      # Other options: "null" or "error".
      unknown: "raw"
    Horizontal accuracy: "col:HORIZONTAL_ACCURACY"
    Vertical Accuracy: null
    Horizontal Speed: null
    Vertical Speed: null
    Horizontal speed accuracy: null
    Vertical speed accuracy: null
    Entity-position link: null
    Entity-Timestamp link: null
    spatial-temporal link: null

  # timestamp_lower + timestamp_upper creates one interval assertion.
  - timestamp_lower: "col:FIRST_SEEN"
    timestamp_upper: "col:LAST_SEEN"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:continuous_during_interval"
    Timestamp accuracy: null
    raw_timestamp: null
    temporal_source: "value:internal_clock"
    Altitude: null
    raw_position: null
    position_source: "value:fused"
    Horizontal accuracy: "col:HORIZONTAL_ACCURACY"
    Vertical Accuracy: null
    Horizontal Speed: null
    Vertical Speed: null
    Horizontal speed accuracy: null
    Vertical speed accuracy: null
    Entity-position link: "value:inferred"
    Entity-Timestamp link: "value:bounded_by_source_interval"
    spatial-temporal link: "value:continuous_presence"
```

## Field Reference

| YAML field | Type | Unit | Example | Description |
| --- | --- | --- | --- | --- |
| `name` | string | none | `AXIOM iOS - Cached Locations` | Human-readable preset name. |
| `version` | string | none | `1.0` | Preset version. |
| `parser.name` | string | none | `axiom_ios_cached_locations` | Stable parser identifier recorded in traceability. |
| `parser.version` | string | none | `1.0` | Parser/preset version recorded in traceability. |
| `selectors` | list | none | see example | Source matching rules. At least one selector is required. |
| `extract` | mapping | none | see example | Source extraction settings by format. |
| `expected_columns` | list of strings | source column names | `["LATITUDE"]` | Expected source columns. Drift creates warnings. |
| `timestamp.unix_ms.expression` | string | returns milliseconds | `int(value * 1000)` | Restricted expression used to convert source timestamp values to Unix ms. |
| `tool_label` | mapping | none | `csv.from: file_name` | Per-format rule for the `Tool label` output column. |
| `source_raw_data.source_column` | string or null | source column name | `RAW_PATH` | Source column copied into `Source raw data`. |
| `source_raw_data.source_value` | scalar or null | source-defined | `/private/var/...` | Constant copied into `Source raw data`. |
| `model_mapping` | list | none | see example | Common model fields copied into every generated assertion. |
| `location_mappings` | list | none | see example | Per-assertion mapping rules. Each item can generate one or more rows. |

## Mapping Value Syntax

`model_mapping` uses explicit `source_column` and `source_value` keys.

`location_mappings` can use compact field values:

| Syntax | Type | Example | Meaning |
| --- | --- | --- | --- |
| `col:<source column>` | string | `col:LATITUDE` | Read a source column. |
| `value:<constant>` | string | `value:instant` | Use a string constant. |
| numeric scalar | integer or float | `1000` | Use a numeric constant. |
| `null` | null | `null` | Leave the output field empty. |

String constants in `location_mappings` must use `value:`. This avoids
confusing a literal string with a source column name.

For encoded source fields, use the explicit mapping form:

```yaml
position_source:
  source_column: "ZTYPE"
  labels:
    1: "GNSS"
    4: "WiFi"
    6: "LTE"
  unknown: "raw"
  keep_source_value: true
```

Working logic:

- `source_column` reads the original source value.
- `labels` maps source values to model values. YAML numeric keys such as `1`
  and string keys such as `"1"` are treated equivalently.
- `keep_source_value` defaults to `true`. When enabled, details includes
  `value_map_references` with the source column, original source value, mapped
  value, match status, and unknown-code policy.
- `unknown` defaults to `raw`, meaning an unmapped code is copied into the model
  field and marked as `matched: false` in details. Use `null` to leave the field
  empty for unknown codes, or `error` to stop processing.

The same `labels`, `unknown`, and `keep_source_value` keys are available in
`model_mapping` entries:

```yaml
model_mapping:
  - model_name: "Entity type"
    source_column: "ENTITY_TYPE_CODE"
    labels:
      1: "device"
      2: "account"
```

`raw_timestamp`, `raw_position`, `temporal_source`, and `position_source` are
ordinary model fields. Use them in `location_mappings` when the source can vary
by assertion. Use them in `model_mapping` only when the same value applies to
every assertion generated from a source row. A per-location value overrides a
common `model_mapping` value.

`raw_timestamp` and `raw_position` preserve the original semantic evidence when
the encoded model fields are derived values. For example, a preset can encode
`tomorrow` into interval bounds while keeping `raw_timestamp: "col:MESSAGE_TIME_TEXT"`,
or geocode `I'm at home` while keeping `raw_position: "col:MESSAGE_BODY"`.

## Location Mapping Logic

Each item in `location_mappings` produces assertions from each extracted source
row.

### Instant Assertion

```yaml
location_mappings:
  - timestamp: "col:RECORDED_AT"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:instant"
    raw_timestamp: "col:TIME_TEXT"
    temporal_source: "value:NTP"
    raw_position: "col:PLACE_TEXT"
    position_source: "value:GNSS"
```

Output logic:

- lower bound original = `RECORDED_AT` value;
- upper bound original = `RECORDED_AT` value;
- lower bound type = `RECORDED_AT`;
- upper bound type = `RECORDED_AT`;
- lower/upper Unix ms are calculated with `timestamp.unix_ms.expression`;
- one output row is created per source row.

### List of Instant Assertions

```yaml
location_mappings:
  - timestamp:
      - "col:FIRST_SEEN"
      - "col:LAST_SEEN"
      - "col:UPDATED_AT"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:instant"
    raw_timestamp: null
    temporal_source: "col:CLOCK_SOURCE"
    raw_position: "col:WIFI_BSSID"
    position_source: "col:LOCATION_PROVIDER"
```

Output logic:

- one output assertion is created for each timestamp column;
- each assertion is an instant where lower bound equals upper bound;
- type fields contain the source timestamp column name.

### Interval Assertion

```yaml
location_mappings:
  - timestamp_lower: "col:FIRST_SEEN"
    timestamp_upper: "col:LAST_SEEN"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:continuous_during_interval"
    raw_timestamp: null
    temporal_source: "value:internal_clock"
    raw_position: null
    position_source: "value:WiFi"
```

Output logic:

- one output assertion is created per source row;
- lower and upper bound types contain their source column names;
- the preset author must choose lower/upper columns according to the intended
  temporal bounds; the mapper does not silently swap them.

## Temporal Relation Values

Allowed constants for `Temporal relation` are:

- `instant`
- `once_during_interval`
- `sporadic_during_interval`
- `continuous_during_interval`
- `never_during_interval`
- `unknown_during_interval`

If `Temporal relation` is omitted, the mapper defaults to:

- `instant` when every generated assertion has the same lower and upper
  timestamp column;
- `unknown_during_interval` when lower and upper timestamp columns differ.

For clarity and auditability, presets should usually specify the value
explicitly.

## Required Columns

Every source column referenced by `model_mapping`, `location_mappings`,
`tool_label.from: column`, or `source_raw_data.source_column` is required. If a
matched source element does not contain one of those columns, processing raises
an error.

`expected_columns` is a drift warning list only. It does not define required
versus optional fields; required fields are determined by mappings.
