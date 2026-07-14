# Flat CSV migration — old `OUTPUT_COLUMNS` → new projection

Status: **contract, 2026-07-14.** The flat CSV stops being a hand-built 45-column frame
(`model_atlas/model/families.py` `OUTPUT_COLUMNS`) and becomes a **projection query** over
the run database ([run_db_schema.md](run_db_schema.md)). This document is the exact
before→after mapping and the exporter's column contract for task **T50**. It supersedes the
byte-parity oracle: `row_uid` values change (now genuinely unique — the founding defect),
so goldens are **regenerated once, deliberately, and reviewed against this table**.

Projection = join up the grain chain (`assertion → raw_record → source → run → case`,
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
| linked_entity | → | entity.concerns | the attributed party fixed at parsing (D12/D17); `concerns_origin` says who |
| time_lower_raw | → | temporal_component(part=lower).raw_value | verbatim, component row (D15) |
| time_lower_source_field | → | temporal_component(part=lower).source_field | |
| time_lower_unix_utc_us | = | timespan_integration.lower_unix_utc_us | canonical UTC µs |
| time_upper_raw | → | temporal_component(part=upper).raw_value | |
| time_upper_source_field | → | temporal_component(part=upper).source_field | |
| time_upper_unix_utc_us | = | timespan_integration.upper_unix_utc_us | |
| utc_offset_hours | ⇄ | timespan_integration.utc_offset_hours_lower / _upper | applied offset **per bound** (D9); stated offset lives at L1 (temporal_component.stated_offset) |
| time_accuracy_us | = | timespan_integration.time_accuracy_us | |
| temporal_source | = | temporal_expression.temporal_source | clock mechanism |
| latitude_wgs84 | = | spatial_integration.latitude_wgs84 | |
| latitude_source_field | → | spatial_component(part=latitude_wgs84).source_field | |
| longitude_wgs84 | = | spatial_integration.longitude_wgs84 | |
| longitude_source_field | → | spatial_component(part=longitude_wgs84).source_field | |
| altitude_m | = | spatial_integration.altitude_m | |
| position_type | → | spatial_component.spatial_role | its semantics were always the place's role (origin/destination/…) (D25) |
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
| entity_position_link | = | assertion.entity_position_link | |
| entity_time_link | = | assertion.entity_time_link | |
| spatial_temporal_link | = | assertion.spatial_temporal_link | |
| raw_source_path | → | source.data_source (fallback: source.input_file_path!inner_path) | the V1 input-source vs data-source split (D24) |
| input_file_path | = | source.input_file_path | |
| input_record_id | = | raw_record.input_record_id | |
| source_record_uid | → | raw_record.raw_record_id | our surrogate; the artefact's own key is raw_record.native_record_uid |
| row_uid | → | assertion.assertion_id | **values change** — now genuinely unique |
| preset_id | → | preset.key (via assertion.preset_id) | preset is now per-assertion (D27) |
| preset_name | → | preset.name (via assertion.preset_id) | |
| source_label | = | assertion.source_label | the one per-claim provenance field |
| source_tier | → | source.source_tier | source grain (D24) |
| deleted | = | assertion.deleted | the source's own deletion claim (L1) |
| record_type | → | analysis(current method).record_type | analysis layer (D19) |
| record_rank | → | analysis(current method).record_rank | |

## New columns (＋)

| new column | source | why |
| --- | --- | --- |
| case_number | case_metadata.case_number | the investigation root (D28) |
| content_uid | assertion.content_hash | portable, preset+file-scoped digest |
| claim_digest | assertion.claim_digest | file-independent digest — possible-same-evidence finding (R5) |
| raw_identifier | entity.raw_identifier | the identifier as written (D21) |
| identifier_type | entity.identifier_type | which normalization applied (D17) |
| entity_role | assertion.entity_role | the entity's part in the activity |
| concerns_origin | entity.concerns_origin | who attributed `linked_entity` (operator vs preset) |
| spatial_referential | spatial_expression.referential | encoding/CRS: epsg:4326 / geohash / address / cell_ref / … (R6 — mandatory) |
| locates | derived from entity_position_link | self / infrastructure / referenced (R6 — mandatory; D11 derived) |
| spatial_method | spatial_integration.method | the derivation, mandatory (R6) |
| temporal_method | timespan_integration.method | the derivation, mandatory (R6) |
| time_lower_stated_offset / _upper | temporal_component.stated_offset | what the source stated per bound (R13) |
| time_lower_datetime_iso / _upper | timespan_integration.*_datetime_iso | one offset-tagged local ISO; ` N/A` when zone unknown, empty when no instant (D32) |
| time_lower_date / _upper, time_lower_time / _upper | timespan_integration.*_date / *_time | local date + time-of-day (D32) |
| recovery_state | raw_record.recovery_state | how the bytes were obtained — live/WAL/journal (R11); distinct from `deleted` |
| data_source | source.data_source | where the data originally lived (D24) |
| activity_id | assertion.activity_id | **kept** (D35) — regroups the lines referencing the same activity (the legs of one record) |
| activity_kind | activity.kind | call / message / trip / … — **bonus, optional** (D35) |
| counterpart, facets, context | activity.context_json (json_extract) | the record's other parties (D18) — **bonus, optional** (D35): no mandatory column sources from the activity table |
| s2_cell_id | spatial_index.s2_cell_id | optional — the S2 leaf cell (D31) |

## Removed from the CSV (⌫)
None outright — every old column has a home above. Genesis stays a *derived, unstored*
view of `entity_time_link` (unchanged). `attribution_origin` is superseded by
`entity.concerns_origin`.

## Regeneration procedure (T50)
1. Build the projection query to emit the columns above in a fixed, documented order
   (unchanged block first for reviewer diffing, then the new columns, then `orig_<col>`).
2. Run the golden pipeline; **review the diff column-by-column against this table** — an
   expected change is a rename/re-home/new column here; anything else is a bug.
3. Freeze the regenerated goldens once approved. Byte-parity with the pre-refactor CSV is
   retired; the parity that remains is folder==zip → equal `content_uid` / `claim_digest`.
