# Integration Model

The canonical output model is a flat table. One output row represents one
spatio-temporal assertion:

```text
entity X had relation Y to position P during temporal interval T
```

Every temporal assertion is modeled as an interval. An instant is represented by
equal lower and upper temporal bounds.

## Columns

The current CSV uses snake_case column names.

| Column | Type | Unit | Description |
| --- | --- | --- | --- |
| `entity` | string or null | none | Main entity concerned by the assertion. |
| `entity_type` | string or null | none | Type/category of `entity`. |
| `linked_entity` | string or null | none | Optional secondary entity linked to the assertion. |
| `time_lower_raw` | source scalar or null | source-defined | Original source value for the lower temporal bound. |
| `time_lower_source_field` | string or null | source column | Source column that supplied the lower bound. |
| `time_lower_unix_ns` | integer or null | Unix nanoseconds | Normalized lower temporal bound. |
| `time_upper_raw` | source scalar or null | source-defined | Original source value for the upper temporal bound. |
| `time_upper_source_field` | string or null | source column | Source column that supplied the upper bound. |
| `time_upper_unix_ns` | integer or null | Unix nanoseconds | Normalized upper temporal bound. |
| `time_zone` | string or null | none | Timezone declared or extracted for the temporal value. |
| `time_accuracy_ns` | integer or null | nanoseconds | Temporal uncertainty or resolution when known. |
| `temporal_source` | string or null | none | Source/mechanism behind the temporal value. |
| `latitude_wgs84` | float or null | decimal degrees | WGS84 latitude. |
| `longitude_wgs84` | float or null | decimal degrees | WGS84 longitude. |
| `altitude_m` | float or null | meters | Altitude. |
| `position_type` | string or null | none | Source-derived position type when known. |
| `raw_position` | source scalar or null | source-defined | Raw spatial expression or source value. |
| `position_source` | string or null | none | Source/mechanism behind the position. |
| `horizontal_accuracy_m` | float or null | meters | Horizontal spatial uncertainty. |
| `vertical_accuracy_m` | float or null | meters | Vertical spatial uncertainty. |
| `horizontal_speed_kmh` | float or null | km/h | Horizontal speed. |
| `vertical_speed_kmh` | float or null | km/h | Vertical speed. |
| `horizontal_speed_accuracy_kmh` | float or null | km/h | Horizontal speed uncertainty. |
| `vertical_speed_accuracy_kmh` | float or null | km/h | Vertical speed uncertainty. |
| `heading_deg` | float or null | degrees | Direction of travel or heading. |
| `heading_accuracy_deg` | float or null | degrees | Heading uncertainty. |
| `beam_azimuth_deg` | float or null | degrees | Cell/WiFi/radio beam azimuth when known. |
| `beam_width_deg` | float or null | degrees | Cell/WiFi/radio beam width when known. |
| `entity_position_link` | string or null | controlled value | Relation between entity and position. |
| `entity_time_link` | string or null | controlled value | Relation between entity and temporal interval. |
| `spatial_temporal_link` | string or null | controlled value | Relation between position and temporal interval. |
| `acquisition_path` | string or null | path/context | Acquisition or evidence container path when known. |
| `source_file_path` | string or null | path/context | Original source path or internal evidence path when known. |
| `tool_label` | string or null | none | Source/tool label such as table, sheet, or export label. |
| `input_file` | string or null | path/context | Input file processed by the adapter. |
| `record_locator` | string or null | source-defined | Source-local locator useful for manual review. |
| `source_tier` | string or null | controlled value | Source tier, for example primary or secondary. |
| `source_row_id` | string | stable ID | Stable identifier for the source row. |
| `deleted` | string or null | controlled value | Record state when known. |
| `record_type` | string or null | controlled value | Untangle result, usually main/additional. |
| `record_rank` | integer or null | rank | Rank inside comparable assertion groups. |

## Temporal Semantics

`time_lower_*` and `time_upper_*` define the assertion interval. For an instant,
the lower and upper raw values, source fields, and normalized values are the
same.

Timestamp normalization is preset-defined through temporal pipes. The built-in
`parse_datetime` pipe writes Unix nanoseconds.

## Link Fields

The three link fields are separate because evidence can support each relation
differently:

- `entity_position_link`: how the entity is linked to the position.
- `entity_time_link`: how the entity is linked to the time interval.
- `spatial_temporal_link`: how the position is linked to the time interval.

These fields are controlled vocabularies in the model layer and are validated
for constant preset values.

## Entity Fields

`entity` and `linked_entity` may be mapped by a preset or supplied at run time via
`--entity` and `--linked-entity`. A run-level argument is authoritative: when given
it overrides the preset's value, and the preset mapping is the default used only
when the argument is absent.

## Provenance Fields

Preset mappings may set provenance fields directly. Otherwise the pipeline fills
available defaults for:

- `acquisition_path`
- `source_file_path`
- `input_file`
- `source_tier`

Traceability and warning sidecars contain run-level provenance, matched preset
information, row counts, and source frontier information.

## Untangle Fields

Untangle compares rows that have the same entity and temporal grouping keys. It
prefers smaller `horizontal_accuracy_m`, then greater field completeness, then
original order.

The result is written to:

- `record_type`
- `record_rank`
