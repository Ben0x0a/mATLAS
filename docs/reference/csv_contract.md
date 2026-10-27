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
| utc_offset_hours | ⇄ | timespan_integration.offset_to_local_hours_lower / _upper | **renamed AND redefined 2026-09-07.** No longer the offset consumed to reach UTC (which was NULL for every epoch source, so the configured zone reached the analyst on no row at all) — it is now the offset to ADD to the canonical UTC µs to read that bound in the case's `local_zone`, per bound, DST-resolved. The source's own offset stays at L1 in `temporal_component.stated_offset` |
| lower_accuracy_us | = | timespan_integration.lower_accuracy_us | per-bound timing uncertainty (µs) |
| upper_accuracy_us | = | timespan_integration.upper_accuracy_us | |
| temporal_source | = | temporal_expression.temporal_source | clock mechanism |
| latitude_wgs84 | = | spatial_integration.latitude_wgs84 | canonical L2 column (kept); L1 part is the datum-agnostic `latitude` |
| latitude_source_field | → | spatial_component(part=latitude).source_field | L1 part is datum-agnostic |
| longitude_wgs84 | = | spatial_integration.longitude_wgs84 | canonical L2 column (kept); L1 part is the datum-agnostic `longitude` |
| longitude_source_field | → | spatial_component(part=longitude).source_field | L1 part is datum-agnostic |
| altitude_m | = | spatial_integration.altitude_m | |
| position_type | ⌫ | — | **removed** (2026-08-30): the role was a preset-author assertion, not the source's words. `*_source_field` names the column the value came from, which carries the same meaning objectively |
| position_raw | → | spatial_integration.position_raw | The verbatim information that ALLOWED `latitude_wgs84`/`longitude_wgs84`, as `<source column>:<value>` joined by ` \| ` — e.g. `ZLATITUDE:46.193000 \| ZLONGITUDE:6.129000`. Scoped to what the producing METHOD read, so a geocoder records an address and a cell/BSSID lookup an identifier. Empty when no coordinate was produced: an un-integrated address or cell reference has no verbatim place in the CSV, and the source's own words stay at L1 in `spatial_component`. Nothing parses it — checking it against the coordinates is an analyst's move |
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
| entity_link | → | observation.entity_link | ONE link per arm (decision 55). *What role* the targeted entity plays: subject (it holds the position, so it is its own bearer) \| party (another entity holds it) \| mentioned \| unknown (no bearer) |
| spatial_link | → | observation.spatial_link | *How the observation uses the position*: measured \| registered \| served \| seen \| used \| stayed \| located \| searched \| shared \| cached \| unknown — an open list. No negative member; precision is `horizontal_accuracy_m` |
| temporal_link | → | observation.temporal_link | *How the interval is referenced*: throughout (□P) \| at_least_once (◇P) \| never (□¬P) \| not_throughout (◇¬P) \| unknown. The ONLY place a negation is stated; on an instant only `at_least_once` / `never` are written |
| recorded_at | → | observation.recorded_at | when the RECORD was written, distinct from when the phenomenon happened. Feeds neither digest |
| raw_source_path | → | source_file.reported_data_source (fallback: source_file.input_file_path!inner_path) | the input-source vs reported-data-source split; NULL for a primary acquisition, so the fallback IS the answer there |
| input_file_path | = | source_file.input_file_path | |
| input_record_id | = | raw_record.input_record_id | |
| source_record_uid | → | raw_record.raw_record_id | our surrogate; the artefact's own key is raw_record.native_record_uid |
| row_uid | → | observation.observation_id | **values change** — now genuinely unique |
| preset_id | → | preset.key (via observation.preset_id) | preset is now per-observation |
| preset_name | → | preset.name (via observation.preset_id) | |
| source_label | = | observation.source_label | the one per-claim provenance field |
| source_tier | → | source_file.source_tier | source grain |
| record_type | → | analysis(current method).**record_rank_type** | analysis layer; **renamed** — it labels the rank (main/additional), which a bare `record_type` did not convey |
| record_rank | → | analysis(current method).record_rank | now emitted **before** `record_rank_type`, so the rank reads before its label |

## New columns (＋)

| new column | source | why |
| --- | --- | --- |
| case_number | case_metadata.case_number | the investigation root |
| observation_content_uid | observation.observation_content_hash | portable, preset+file-scoped digest |
| spatiotemporal_entity_hash | observation.spatiotemporal_entity_hash | file-independent digest — possible-same-evidence finding |
| raw_identifier | entity.raw_identifier | the identifier as written |
| identifier_type | entity.identifier_type | which normalization applied |
| activity_role | observation.activity_role | the TARGETED entity's part in the activity (caller \| callee \| sender \| recipient \| unknown). `subject` is gone — it is now the plain meaning of the entity column |
| concerns_origin | entity.concerns_origin | who attributed `concerns` (operator vs preset) |
| spatial_referential | spatial_expression.referential | encoding/CRS: epsg:4326 / geohash / address / cell_ref / … (mandatory) |
| bearer_entity | entity.identifier (via observation.bearer_entity_id) | the POSITION BEARER's normalised identifier — the antenna, camera or terminal whose position the row states. Repeats the entity for `subject`; empty for `unknown` |
| bearer_raw_identifier | entity.raw_identifier (bearer) | the bearer's verbatim source text |
| bearer_identifier_type | entity.identifier_type (bearer) | which normalisation applied to the bearer |
| bearer_entity_type | entity.type (bearer) | what the bearer is (antenna \| camera \| poi \| …) |
| spatial_method | spatial_integration.method | the derivation, mandatory |
| temporal_method | timespan_integration.method | the derivation, mandatory |
| time_lower_stated_offset / _upper | temporal_component.stated_offset | what the source stated per bound |
| recovery_state | observation.recovery_state | **two values only**: live \| recovered. From our own parsing on a primary source, from the preset (what the tool reports) on a secondary one — strictly tier-gated. Replaced the retired `deleted` column (decision 48) |
| recovery_source | observation.recovery_source | the fine origin — wal/journal/freelist/freespace; ordered by descending reliability, so it doubles as the confidence signal. EMPTY for a live or intact row (recovered from nowhere), so a populated cell IS the "recovered" signal |
| reported_data_source | source_file.reported_data_source | where a SECONDARY tool reports the data originally lived — its claim, not our observation. Empty for a primary acquisition |
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
   equal `observation_content_uid` / `spatiotemporal_entity_hash`.
