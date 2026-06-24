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
| `time_lower_unix_utc_us` | integer or null | Unix microseconds (**UTC**) | Normalized lower temporal bound, in absolute UTC. Local wall-clock = this + `utc_offset_hours`. |
| `time_upper_raw` | source scalar or null | source-defined | Original source value for the upper temporal bound. |
| `time_upper_source_field` | string or null | source column | Source column that supplied the upper bound. |
| `time_upper_unix_utc_us` | integer or null | Unix microseconds (**UTC**) | Normalized upper temporal bound, in absolute UTC. Local wall-clock = this + `utc_offset_hours`. |
| `utc_offset_hours` | float or null | signed hours | The source's UTC offset as a signed-hours float (`0.0`, `2.0`, `6.5`, `-2.5`), by the ISO convention **local = `time_*_unix_utc_us` (UTC) + `utc_offset_hours`**. It is metadata describing how the absolute UTC instant relates to the source's local wall-clock — `null` when the source zone is unknown. (Currently the nominal offset; DST-aware resolution is future work.) |
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
| `raw_source_path` | string or null | path/context | Where the trace came from: a device path (e.g. `preset(path)`) or a tool's source column. Mapped by the preset; defaults to the inner-container logical path. |
| `input_file_path` | string or null | path/context | Full filesystem path of the outermost on-disk artifact mATLAS opened (the input archive when reading from one, else the leaf file). Engine-set. |
| `input_record_id` | string or null | source-defined | Which record in that file: the 1-based source line number `<table-or-sheet>#<line>` by default, or a mapped tool locator. |
| `source_record_uid` | string | stable ID | Per-source-record UID, shared by every output row of one source record. The tool's own id when the preset maps `source_record_uid` (verbatim), else a content-addressed `uuid5` keyed on the source line number. |
| `row_uid` | string | stable ID | Unique per OUTPUT row. Engine-generated deterministic `uuid5` over the output row's own model data + the source line number + output ordinal (scoped by the source); content-addressed, never preset-mapped. |
| `preset_id` | string or null | none | Stable machine id of the applied preset. Engine-set. |
| `preset_name` | string or null | none | Human title of the applied preset. Engine-set. |
| `source_label` | string or null | none | Tool label (secondary) or any descriptive label (primary), e.g. a table name. |
| `source_tier` | string or null | controlled value | Source tier, for example primary or secondary. |
| `deleted` | string or null | controlled value | Record state when known. |
| `record_type` | string or null | controlled value | Untangle result, usually main/additional. |
| `record_rank` | integer or null | rank | Rank inside comparable assertion groups. |

## Source-Column Passthrough (`orig_` columns)

By default every original source column is carried through to the output verbatim,
appended **after** the canonical columns and prefixed `orig_` (e.g. `orig_Latitude`,
`orig_City`). This preserves the untouched source row beside the normalized model, so a
transform can be verified at a glance — e.g. a normalized `horizontal_speed_kmh` of
`10.8` next to its `orig_Speed (m/s)` of `3.0`.

Notes:

- The prefix is `orig_`, not `raw_`, because `raw_position` and `raw_source_path` are
  already canonical columns. A source column whose `orig_` name would still collide with
  a canonical column is disambiguated with a trailing `_`.
- All source columns are included — both mapped and unmapped — so mapped values appear
  twice (once normalized, once verbatim). The frontier report still lists only the
  *unmapped* column names.
- For a **pivoted** source row (one row that fans out into several assertions, e.g.
  Significant Locations' visit interval + creation instant), every output row repeats the
  same `orig_` values.
- In a merged CSV across presets with different source columns, the `orig_` set is the
  union; a row from a source lacking a column is left blank there.
- Disable with the CLI flag `--no-source-columns` (or untick **Include source columns**
  in the GUI) to emit the canonical columns only.

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

### The three record identifiers at a glance

| Column | Granularity | Job |
| --- | --- | --- |
| `input_record_id` | one per source line | **Trace back** to the input line (the 1-based source line number by default, or a mapped tool locator). |
| `source_record_uid` | one per source line | **Group** the output rows that came from the same source record (one record can fan out into several rows). |
| `row_uid` | one per output line | **Distinguish / count** every emitted output row — unique across the whole output. |

In short: `input_record_id` points back to the source, `source_record_uid` links the rows
of one source record, and `row_uid` is the unique key of each output row.

Provenance answers four questions in order: *where from* (`raw_source_path` →
`input_file_path` → `input_record_id`), *which record and its keys*
(`input_record_id` for a human, `source_record_uid` as a deterministic per-source-record
machine key, `row_uid` as the unique per-output-row key), *how it was read* (`preset_id` /
`preset_name` / `source_label` / `source_tier`), and *record state* (`deleted`).

A preset maps `raw_source_path`, `input_record_id`, `source_label`, `deleted` and
(optionally) `source_record_uid`. The engine always sets `input_file_path`, `preset_id`,
`preset_name`, `row_uid`; it fills `input_record_id` (the 1-based source line number,
`<table-or-sheet>#<line>`) and `source_record_uid` (a content-addressed `uuid5`) when the
preset maps neither, defaults `raw_source_path` to the inner-container logical path, and
`source_tier` from the preset tier.

`source_record_uid` is the source record's stable key, shared by every output row fanned
out from one source record. When a preset maps it to a tool's id column it is used
verbatim, so output rows join straight back to the tool artefact. Otherwise it is
`uuid5(content_fingerprint | raw_source_path | source_line_number)` — globally unique
(two acquisitions differ in the fingerprint; identical-data records differ in the line
number), independent of the input file name so a db read from a folder or a zip yields the
same UID, and portable across machines. A *mapped* duplicate `source_record_uid` across
distinct source records is a hard error (a non-unique "stable id" is a preset bug); the
generated one never collides because it is keyed on the unique source line number. The
line number is the physical position, not the (possibly mapped) `input_record_id` value.
`row_uid` is then derived per output row
(`uuid5(content_fingerprint | raw_source_path | source_line_number | output_model_data | output_ordinal)`),
so every emitted row has a unique, reproducible, content-addressed key — folding in the
row's own model data while the line number keeps identical-data records distinct and the
output ordinal keeps a record's fan-out rows distinct.

Traceability and warning sidecars contain run-level provenance, matched preset
information, row counts, and source frontier information.

## Untangle Fields

Untangle compares rows that have the same entity and temporal grouping keys. It
prefers smaller `horizontal_accuracy_m`, then greater field completeness, then
original order.

The result is written to:

- `record_type`
- `record_rank`
