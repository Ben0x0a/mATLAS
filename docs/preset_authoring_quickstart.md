# Preset Authoring Quickstart

Use this guide when creating a real preset from an export or database table.
Use [preset_schema.md](preset_schema.md) when you need the full field reference.

## 1. Identify The Source Element

Choose the narrowest selector that still matches the source:

```yaml
selectors:
  - source_type: csv
    file_name: "Cached Locations.csv"
```

For Excel, include both workbook and sheet when possible:

```yaml
selectors:
  - source_type: excel
    file_name: "AXIOM Export.xlsx"
    sheet_name: "Cached Locations"
```

For SQLite inside an evidence archive, use the database path, not the table:

```yaml
selectors:
  - source_type: sqlite
    file_name: "Cache.sqlite"
    db_relpath: "/private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite"
```

## 2. List Expected Columns

Copy the source columns exactly:

```yaml
expected_columns:
  - "DEVICE_ID"
  - "TIMESTAMP"
  - "LATITUDE"
  - "LONGITUDE"
```

This is a drift warning list. Required columns are determined by mappings, not
by `expected_columns`.

## 3. Map Common Entity Fields

Keep common fields small. They are copied into every assertion created from one
source row.

```yaml
model_mapping:
  - model_name: "Entity"
    source_column: "DEVICE_ID"
  - model_name: "Entity type"
    source_value: "device"
  - model_name: "Linked Entity"
    source_column: null
```

## 4. Add Location Assertions

One `location_mappings` item creates one or more output rows from each source
row.

Single instant:

```yaml
location_mappings:
  - timestamp: "col:TIMESTAMP"
    Latitude: "col:LATITUDE"
    Longitude: "col:LONGITUDE"
    Temporal relation: "value:instant"
    raw_timestamp: "col:TIME_TEXT"
    temporal_source: "value:NTP"
    raw_position: "col:PLACE_TEXT"
    position_source: "value:GNSS"
```

Multiple instants at the same position:

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

Encoded source values:

```yaml
location_mappings:
  - timestamp: "col:ZTIMESTAMP"
    Latitude: "col:ZLATITUDE"
    Longitude: "col:ZLONGITUDE"
    position_source:
      source_column: "ZTYPE"
      labels:
        1: "GNSS"
        4: "WiFi"
        6: "LTE"
      unknown: "raw"
```

The model field receives the label. The original code stays in `details` under
`value_map_references`.

Interval state:

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

Trip as two assertions:

```yaml
location_mappings:
  - timestamp: "col:DEPARTURE_TIME"
    Latitude: "col:DEPARTURE_LATITUDE"
    Longitude: "col:DEPARTURE_LONGITUDE"
    Temporal relation: "value:instant"
    raw_timestamp: null
    temporal_source: "value:internal_clock"
    raw_position: null
    position_source: "value:GNSS"

  - timestamp: "col:ARRIVAL_TIME"
    Latitude: "col:ARRIVAL_LATITUDE"
    Longitude: "col:ARRIVAL_LONGITUDE"
    Temporal relation: "value:instant"
    raw_timestamp: null
    temporal_source: "value:internal_clock"
    raw_position: null
    position_source: "value:GNSS"
```

## 5. Validate Before Processing

Run a first pass with a small input set to check selector and mapping output:

```bash
python matlas.py process --input ./evidence --presets ./presets --output ./out/test.csv
```

Inspect the `test.matlas.warnings.json` sidecar to confirm column drift and
frontier results. Only run against the full evidence set once selectors and
mappings look right.
