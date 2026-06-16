# Transform Behavior

Transforms run after extraction and before export.

The class-level orchestration entry point is
`SpatioTemporalProcessor.transform(input_path, presets_path, output_csv, ...)`.
It creates the request, discovers/matches sources, maps rows into assertions,
then writes CSV, warning, and traceability sidecars.

Order:

1. Validate expected and mapped source columns.
2. Apply common `model_mapping` fields.
3. Expand `location_mappings` into internal `SpatioTemporalAssertion` objects.
4. Calculate lower/upper timestamp interval bounds in Unix milliseconds.
5. Flatten assertions to the canonical CSV column model.
6. Add provenance and tool label fields.
7. Preserve unmapped details.
8. Optionally append rows from an existing integration-model CSV.
9. Apply untangle over the combined model.

## Internal Assertion Model

The transformation layer uses typed domain objects before writing a flat CSV:

- `Entity`: entity value, entity type, and linked entity.
- `Temporal`: lower/upper interval bounds, normalized Unix milliseconds,
  timestamp accuracy, raw timestamp, temporal source, and temporal relation.
- `Spatial`: latitude, longitude, altitude, position type, raw position,
  position source, accuracy, and speed fields.
- `Provenance`: source file, original path, raw-data reference, and tool label.
- `EvaluationLinks`: entity-position, entity-timestamp, and spatial-temporal
  evaluation links.
- `RecordRank`: untangle record type and rank.

`SpatioTemporalAssertion.to_flat_row()` is the boundary between the internal
object model and the CSV representation. The CSV remains flat and stable.

## Location Expansion

Each `location_mappings` item creates one or more assertions per source row.

- `timestamp: "col:RECORDED_AT"` creates one instant assertion.
- `timestamp: ["col:FIRST_SEEN", "col:LAST_SEEN"]` creates one instant
  assertion per timestamp column.
- `timestamp_lower` + `timestamp_upper` creates one interval assertion.

The mapper writes source timestamp column names into:

- `Timestamp interval lower bound type`
- `Timestamp interval upper bound type`

It does not replace those types with convention names. This keeps source
semantics traceable.

`raw_timestamp`, `raw_position`, `temporal_source`, and `position_source` are
mapped like other assertion fields. Use constants such as `value:NTP`,
`value:internal_clock`, `value:GNSS`, or `value:WiFi`, or map source columns
with `col:<source column>`.

Encoded source fields can be mapped with preset `labels`. For example, a source
column `ZTYPE` can map `1` to `GNSS`, `4` to `WiFi`, and `6` to `LTE`. The
mapped label is written to the model field, and the original source value is
kept in details under `value_map_references` unless the preset sets
`keep_source_value: false`.

## Details Modes

- `json`: one `details` JSON column with unmapped source columns.
- `append_column`: append unmapped fields as `details_<source column>`.

The mapper also adds parser details such as:

- `positions_source_columns`
- `timestamp_interval_source_columns`
- `value_map_references`

## Append

`--append-model` loads an existing integration-model CSV and concatenates it
before the newly mapped rows. No deduplication is attempted. This preserves
evidence from tools such as AXIOM and from direct sources side by side, even
when they describe the same apparent location event.

`Record type` and `Record rank` are recalculated after append so the untangle
view reflects the complete output model.

## Untangle

Untangle groups rows by:

- `Timestamp interval lower bound UNIX ms` truncated to seconds;
- `Timestamp interval upper bound UNIX ms` truncated to seconds;
- `Entity`;
- `Timestamp interval lower bound type`;
- `Timestamp interval upper bound type`.

It ranks by canonical `Horizontal accuracy`, smallest value first. A row with a
numeric accuracy beats a row with no numeric accuracy. When accuracy cannot
decide because values are equal or both missing, the tie-breaker is the number
of available useful fields.

Useful fields exclude source, metadata, and type columns. They include core
spatio-temporal values and the link fields:

- `Entity`
- `Linked Entity`
- `Timestamp interval lower bound original`
- `Timestamp interval lower bound UNIX ms`
- `Timestamp interval upper bound original`
- `Timestamp interval upper bound UNIX ms`
- `Timestamp accuracy`
- `raw_timestamp`
- `temporal_source`
- `Temporal relation`
- `Latitude`
- `Longitude`
- `Altitude`
- `raw_position`
- `position_source`
- `Horizontal accuracy`
- `Vertical Accuracy`
- `Horizontal Speed`
- `Vertical Speed`
- `Horizontal speed accuracy`
- `Vertical speed accuracy`
- `Entity-position link`
- `Entity-Timestamp link`
- `spatial-temporal link`

If accuracy and completeness are still tied, original row order is kept.
