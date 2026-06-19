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
| `time_lower_unix_us` | integer or null | Unix microseconds | Normalized lower temporal bound. |
| `time_upper_raw` | source scalar or null | source-defined | Original source value for the upper temporal bound. |
| `time_upper_source_field` | string or null | source column | Source column that supplied the upper bound. |
| `time_upper_unix_us` | integer or null | Unix microseconds | Normalized upper temporal bound. |
| `time_zone` | string or null | none | Timezone declared or extracted for the temporal value. |
| `time_accuracy_us` | integer or null | microseconds | Temporal uncertainty or resolution when known. |
| `temporal_source` | string or null | none | Source/mechanism behind the temporal value. |
| `latitude_wgs84` | float or null | decimal degrees | WGS84 latitude. |
| `latitude_source_field` | string or null | source column | Source column latitude was read from (auto-captured). |
| `longitude_wgs84` | float or null | decimal degrees | WGS84 longitude. |
| `longitude_source_field` | string or null | source column | Source column longitude was read from (auto-captured). |
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
| `raw_source_path` | string or null | path/context | Where the trace came from: a device path (e.g. `preset(in_archive)`) or a tool's source column. Mapped by the preset. |
| `input_file` | string or null | path/context | The specific file mATLAS read (the zip / CSV / workbook). Engine-set. |
| `input_record_id` | string or null | source-defined | Which record in that file: `<table-or-sheet>#<ordinal>` by default, or a tool locator. |
| `record_uid` | string | stable ID | Deterministic, globally-unique UID. The tool's own id when the preset maps `record_uid` (verbatim), else a content-addressed `uuid5`. |
| `preset_id` | string or null | none | Stable machine id of the applied preset. Engine-set. |
| `preset_name` | string or null | none | Human title of the applied preset. Engine-set. |
| `source_label` | string or null | none | Tool label (secondary) or any descriptive label (primary), e.g. a table name. |
| `source_tier` | string or null | controlled value | Source tier, for example primary or secondary. |
| `deleted` | string or null | controlled value | Record state when known. |
| `record_type` | string or null | controlled value | Untangle result, usually main/additional. |
| `record_rank` | integer or null | rank | Rank inside comparable assertion groups. |

## Temporal Semantics

`time_lower_*` and `time_upper_*` define the assertion interval. For an instant,
the lower and upper raw values, source fields, and normalized values are the
same.

Timestamp normalization is preset-defined through temporal pipes. The built-in
`parse_datetime` pipe writes Unix microseconds.

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

Provenance answers four questions in order: *where from* (`raw_source_path` →
`input_file` → `input_record_id`), *which record and its stable key*
(`input_record_id` for a human, `record_uid` as a deterministic machine key), *how it
was read* (`preset_id` / `preset_name` / `source_label` / `source_tier`), and *record
state* (`deleted`).

A preset maps `raw_source_path`, `input_record_id`, `source_label`, `deleted` and
(optionally) `record_uid`. The engine always sets `input_file`, `preset_id`,
`preset_name`; it fills `input_record_id` (`<table-or-sheet>#<ordinal>`) and
`record_uid` (a content-addressed `uuid5`) when the preset maps neither, and defaults
`source_tier` from the preset tier.

`record_uid` is the row's stable key, shared by every assertion fanned out from one
source row. When a preset maps `record_uid` to a tool's id column it is used verbatim,
so output rows join straight back to the tool artefact. Otherwise it is
`uuid5(content_fingerprint | raw_source_path | input_file | input_record_id)` — globally
unique (two acquisitions differ in the content fingerprint) and portable across machines.
A duplicate `record_uid` across distinct source rows is a hard error.

Traceability and warning sidecars contain run-level provenance, matched preset
information, row counts, and source frontier information.

## Untangle Fields

Untangle compares rows that have the same entity and temporal grouping keys. It
prefers smaller `horizontal_accuracy_m`, then greater field completeness, then
original order.

The result is written to:

- `record_type`
- `record_rank`
