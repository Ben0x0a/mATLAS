# Flat CSV column contract

The flat CSV is a **projection query** over the run database (see
[../model/schema.md](../model/schema.md)) — one row per observation — not a hand-built frame.
This document is the exporter's column contract: the exact set of columns, their order, and
what each one carries.

Projection = join up the grain chain (`observation → raw_record → source → run → case`,
plus the two owned corners and their preferred integrations, plus `analysis` for the
current ranking method). The `orig_<col>` verbatim passthrough is re-emitted from
`raw_record.raw_json` exactly as today (a hard forensic constraint).

## Legend
- **=** unchanged name and meaning · **→** renamed / re-homed · **⇄** split into several ·
  **＋** new column (no old counterpart) · **⌫** removed from the flat CSV.

## Every old column, mapped

| old CSV column | status | new source (table.column) | note |
| --- | --- | --- | --- |
| entity | → | entity.identifier | now the **normalized** identifier; the verbatim form is the new `raw_identifier` column |
| entity_type | ⇄ | entity.identifier_type + entity.type | `identifier_type` = which normalization; `type` = what the thing is (device/person/…) |
| concerns | → | entity.concerns | the attributed party fixed at parsing; `concerns_origin` says who |
| time_lower_raw | → | temporal_component(part=lower).raw_value | verbatim, component row |
| time_lower_source_field | → | temporal_component(part=lower).source_field | |
| time_lower_unix_utc_us | = | timespan_integration.lower_unix_utc_us | canonical UTC µs |
| time_upper_raw | → | temporal_component(part=upper).raw_value | |
| time_upper_source_field | → | temporal_component(part=upper).source_field | |
| time_upper_unix_utc_us | = | timespan_integration.upper_unix_utc_us | |
| utc_offset_hours | ⇄ | timespan_integration.utc_offset_hours_lower / _upper | applied offset **per bound**; stated offset lives at L1 (temporal_component.stated_offset) |
| lower_accuracy_us | = | timespan_integration.lower_accuracy_us | per-bound timing uncertainty (µs) |
| upper_accuracy_us | = | timespan_integration.upper_accuracy_us | |
| temporal_source | = | temporal_expression.temporal_source | clock mechanism |
| latitude_wgs84 | = | spatial_integration.latitude_wgs84 | canonical L2 column (kept); L1 part is the datum-agnostic `latitude` |
| latitude_source_field | → | spatial_component(part=latitude).source_field | L1 part is datum-agnostic |
| longitude_wgs84 | = | spatial_integration.longitude_wgs84 | canonical L2 column (kept); L1 part is the datum-agnostic `longitude` |
| longitude_source_field | → | spatial_component(part=longitude).source_field | L1 part is datum-agnostic |
| altitude_m | = | spatial_integration.altitude_m | |
| position_type | → | spatial_component.spatial_role | its semantics were always the place's role (origin/destination/…) |
| raw_position | → | spatial_component(part=raw_position).raw_value | verbatim composite |
| position_source | → | spatial_expression.spatial_source | mechanism (GNSS/WiFi/cell), symmetric with temporal_source |
| horizontal_accuracy_m | = | spatial_integration.horizontal_accuracy_m | |
| vertical_accuracy_m | = | spatial_integration.vertical_accuracy_m | |
| horizontal_speed_kmh | = | spatial_integration.horizontal_speed_kmh | |
| vertical_speed_kmh | = | spatial_integration.vertical_speed_kmh | |
| horizontal_speed_accuracy_kmh | = | spatial_integration.horizontal_speed_accuracy_kmh | |
| vertical_speed_accuracy_kmh | = | spatial_integration.vertical_speed_accuracy_kmh | |
| heading_deg | = | spatial_integration.heading_deg | |
| heading_accuracy_deg | = | spatial_integration.heading_accuracy_deg | |
| beam_azimuth_deg | = | spatial_integration.beam_azimuth_deg | |
| beam_width_deg | = | spatial_integration.beam_width_deg | |
| entity_position_link | = | observation.entity_position_link | |
| entity_time_link | = | observation.entity_time_link | |
| spatial_temporal_link | = | observation.spatial_temporal_link | |
| raw_source_path | → | source.data_source (fallback: source.input_file_path!inner_path) | the input-source vs data-source split |
| input_file_path | = | source.input_file_path | |
| input_record_id | = | raw_record.input_record_id | |
| source_record_uid | → | raw_record.raw_record_id | our surrogate; the artefact's own key is raw_record.native_record_uid |
| row_uid | → | observation.observation_id | **values change** — now genuinely unique |
| preset_id | → | preset.key (via observation.preset_id) | preset is now per-observation |
| preset_name | → | preset.name (via observation.preset_id) | |
| source_label | = | observation.source_label | the one per-claim provenance field |
| source_tier | → | source.source_tier | source grain |
| deleted | = | observation.deleted | the source's own deletion claim (L1) |
| record_type | → | analysis(current method).record_type | analysis layer |
| record_rank | → | analysis(current method).record_rank | |

## New columns (＋)

| new column | source | why |
| --- | --- | --- |
| case_number | case_metadata.case_number | the investigation root |
| content_uid | observation.content_hash | portable, preset+file-scoped digest |
| claim_digest | observation.claim_digest | file-independent digest — possible-same-evidence finding |
| raw_identifier | entity.raw_identifier | the identifier as written |
| identifier_type | entity.identifier_type | which normalization applied |
| entity_role | observation.entity_role | the entity's part in the activity |
| concerns_origin | entity.concerns_origin | who attributed `concerns` (operator vs preset) |
| spatial_referential | spatial_expression.referential | encoding/CRS: epsg:4326 / geohash / address / cell_ref / … (mandatory) |
| locates | derived from entity_position_link | self / infrastructure / referenced (mandatory; derived) |
| spatial_method | spatial_integration.method | the derivation, mandatory |
| temporal_method | timespan_integration.method | the derivation, mandatory |
| time_lower_stated_offset / _upper | temporal_component.stated_offset | what the source stated per bound |
| time_lower_datetime_iso / _upper | timespan_integration.*_datetime_iso | one offset-tagged local ISO; ` N/A` when zone unknown, empty when no instant |
| time_lower_date / _upper, time_lower_time / _upper | timespan_integration.*_date / *_time | local date + time-of-day |
| recovery_state | raw_record.recovery_state | the coarse state — live vs recovered; distinct from `deleted` |
| recovery_source | raw_record.recovery_source | the fine origin — live/wal/journal/freelist/freespace; ordered by descending reliability, so it doubles as the confidence signal |
| data_source | source.data_source | where the data originally lived |
| activity_id | observation.activity_id | **kept** — regroups the lines referencing the same activity (the legs of one record) |
| activity_kind | activity.kind | call / message / trip / … — **bonus, optional** |
| counterpart, facets, context | activity.context_json (json_extract) | the record's other parties — **bonus, optional**: no mandatory column sources from the activity table |
| s2_cell_id | spatial_index.s2_cell_id | optional — the S2 leaf cell |

## Removed from the CSV (⌫)
None outright — every old column has a home above. Genesis stays a *derived, unstored*
view of `entity_time_link` (unchanged). `attribution_origin` is superseded by
`entity.concerns_origin`.

## Regenerating the golden CSVs
1. The projection query emits the columns above in a fixed, documented order (the canonical
   block first, then any new columns, then the `orig_<col>` tail).
2. Run the golden pipeline; **review the diff column-by-column against this table** — an
   expected change is a rename, re-home, or new column here; anything else is a bug.
3. Freeze the regenerated goldens once approved. The parity that holds is folder == zip →
   equal `content_uid` / `claim_digest`.
