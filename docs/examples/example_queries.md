# Example run database — worked queries against the model

A demonstration artefact showing real queries against the run database (see
[../model/schema.md](../model/schema.md)). Built by
[build_example_db.py](build_example_db.py) — deterministic, so rerunning it regenerates
[matlas_example.sqlite](matlas_example.sqlite) byte-for-byte apart from the
`case_metadata`/`run` uuid rows and the run-scoped metadata that carries the run id
(`run_archive`/`run_preset` links, `source.run_id`, `edit_log`). The DDL is hand-written here
for demonstration; the shipped schema is generated from the code.

**The story**: one night in Geneva. The suspect's phone (exhibit A) holds GPS fixes, a
home visit, a chat pin "meet me here" (scene coordinates, the app flagged it deleted)
and a route search to the scene address. Operator A's CDRs place the caller on the tower
next to the scene at 02:20 — operator B disclosed the same call (B-leg). ANPR read the
suspect's plate near the scene at 02:05 and 02:52. CCTV at the scene recorded
02:25-02:50. The robbery (police register) ran 02:30-02:45. A witness saw "l'homme" near
the station around noon, and an alibi statement claims the suspect's phone was **not**
at home during the robbery window (a `never_during_interval` observation). The
cache.sqlite was ingested twice (folder + zip) and one CDR record appears in two
disclosures — both duplications are *findings*, never merges. The projection follows the
[../reference/csv_contract.md](../reference/csv_contract.md) column contract: `entity` is the
**normalised** identifier of the TARGETED entity, `raw_identifier` the verbatim source text;
the `bearer_*` columns come from the SECOND entity join and carry the POSITION BEARER — the
antenna a phone was served by, the camera that saw a plate — which is why "which other
handsets hit this cell?" is now a `GROUP BY` rather than a string match. `is_presence` is
derived from `spatial_link` (`SpatialLink.is_presence`, declared once in the vocabulary);
the context columns (`counterpart`/`callee`/`imei`) are projected from
`activity.context_json` (only `observation.activity_id`, which regroups the legs of one
record, is a useful projection of the activity); `t_lower`/`t_upper` are the offset-tagged
local `datetime_iso` (` N/A` = value, zone unknown, kept distinct from empty = no instant);
`s2_cell_id` is the signed-int64 S2 leaf cell.

Notable in the results:
- **Q1**: line = placement. The bearer is a *column*, never a line — storage orders by whose
  position it is, the projection by who is under investigation. Context parties stay columns
  too; the doubled iOS cache lines are the deliberate double-ingest (see Q5). No
  `SELECT DISTINCT` anywhere — duplicate evidence must SURFACE, never be silently collapsed.
- **Q3 vs Q4**: presence is now ONE test on ONE column (`spatial_link IN (at, within,
  near)`). It used to take three tokens across two enums, because negation was storable in
  two places at once with no rule for what a disagreement meant. The chat pin and the nav
  search (`references`) and the alibi (`absent`) are excluded by that single predicate —
  and they remain distinguishable from each other, which is the point of splitting the three
  negatives.
- **Q5**: both duplicate classes — same file bytes (folder==zip) and the file-independent
  `claim_digest` catching the overlapping CDR disclosure.
- **Q6**: convergence by the normalized `identifier` (`GROUP BY`, never a stored merge),
  with the verbatim `raw_identifier` surfaced beside it.
- **Q9**: the per-bound projection — the source's stated offset (L1) beside the applied
  offset + local `datetime_iso` (L2), and the ` N/A` assumed-UTC sentinel.

*Note: the CDR and ANPR rows carry no coordinates in this build — the cell/camera register
resolution is a work-in-progress stub (`integrate/spatial.py`), so those bearers have an
identity but not yet a position. Q3 therefore returns only the GNSS-located rows.*

### Q1 - The flat projection (one line per placement); the whole example run

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | entity_link | spatial_link | occupancy | attestation | bearer_entity | bearer_entity_type | is_presence | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 64962225 | GE123456 | GE 123 456 | plate | vehicle | unknown |  |  |  | cell_ref | seen_by | near | once | instrument | CAM-B2-KM03 | camera | 1 |  |  |  | 2026-03-12T17:22:05+01:00 | 2026-03-12T17:22:05+01:00 | ANPR |
| 0c8a843f | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-70110 | antenna | 1 |  |  |  | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 | CDR op-A |
| 384f9922 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T19:00:00 N/A | iOS cache |
| c9f6c177 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T19:00:00 N/A | iOS cache |
| 14baf9de |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | continuous | instrument |  |  | 1 | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T22:30:00 N/A | iOS visits |
| 2fb24ac8 | jdoe@gmail.com | jdoe@gmail.com | email | account | unknown |  |  |  | address | searched_for | references | once | system_record | Rue du Stand 15, Geneve | poi | 0 |  |  |  | 2026-03-13T21:04:09+00:00 | 2026-03-13T21:04:09+00:00 | nav search |
| f0432639 | jdoe@gmail.com | jdoe@gmail.com | email | account | unknown |  |  |  | place_name | searched_for | references | once | system_record | Home | poi | 0 |  |  |  | 2026-03-13T21:04:09+00:00 | 2026-03-13T21:04:09+00:00 | nav search |
| 00a7111e |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13T22:00:00 N/A | 2026-03-13T22:00:00 N/A | iOS cache |
| 8dbdec91 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13T22:00:00 N/A | 2026-03-13T22:00:00 N/A | iOS cache |
| 5c8cdc2e |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14T00:58:00 N/A | 2026-03-14T00:58:00 N/A | iOS cache |
| 8fc1b628 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14T00:58:00 N/A | 2026-03-14T00:58:00 N/A | iOS cache |
| 4ee39620 | GE123456 | GE 123 456 | plate | vehicle | unknown |  |  |  | cell_ref | seen_by | near | once | instrument | CAM-A7-KM12 | camera | 1 |  |  |  | 2026-03-14T02:05:33+01:00 | 2026-03-14T02:05:33+01:00 | ANPR |
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | shared | references | once | system_record | scene-pin | poi | 0 | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |
| 0e0357eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-77341 | antenna | 1 |  |  |  | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 56456dbd | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-77341 | antenna | 1 |  |  |  | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| a82222a9 | +41762223344 | +41762223344 | msisdn | device | callee | +41791234567 |  |  | cell_ref | served_by | near | continuous | system_record | 228-02-1011-55221 | antenna | 1 |  |  |  | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-B |
| 2de4a044 | Lobby-2 | Lobby-2 | camera | camera | unknown |  |  |  | address | self | at | continuous | instrument |  |  | 1 |  |  |  | 2026-03-14T02:25:00+01:00 | 2026-03-14T02:50:00+01:00 | CCTV |
| 13794c16 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 5dc72691 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 17193524 | +41791234567 | +41791234567 | msisdn | device | unknown |  |  |  | place_name | self | absent | continuous | person |  |  | 0 |  |  |  | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | alibi |
| 3323cdf9 | GE-2026-04412 | GE-2026-04412 | case_ref | event | unknown |  |  |  | address | self | at | continuous | system_record |  |  | 1 |  |  |  | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | police register |
| c01515e1 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| d99b0e25 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| 904ae0ca | GE123456 | GE 123 456 | plate | vehicle | unknown |  |  |  | cell_ref | seen_by | near | once | instrument | CAM-A7-KM12 | camera | 1 |  |  |  | 2026-03-14T02:52:10+01:00 | 2026-03-14T02:52:10+01:00 | ANPR |
| 8576ae3f |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A | iOS cache |
| bd3b8052 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A | iOS cache |
| 5642495d |  | l'homme | none | person | unknown |  |  |  | place_name | self | at | continuous | person |  |  | 1 |  |  |  | 2026-03-14T11:00:00+01:00 | 2026-03-14T13:00:00+01:00 | audition |
| d7c2b0eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-70110 | antenna | 1 |  |  |  | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 | CDR op-A |

(28 rows)

### Q2 - Timeline of ONE identifier across every source (convergence by GROUP-able normalized identifier, no stored merge)

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | entity_link | spatial_link | occupancy | attestation | bearer_entity | bearer_entity_type | is_presence | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0c8a843f | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-70110 | antenna | 1 |  |  |  | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 | CDR op-A |
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | shared | references | once | system_record | scene-pin | poi | 0 | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |
| 0e0357eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-77341 | antenna | 1 |  |  |  | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 56456dbd | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-77341 | antenna | 1 |  |  |  | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 17193524 | +41791234567 | +41791234567 | msisdn | device | unknown |  |  |  | place_name | self | absent | continuous | person |  |  | 0 |  |  |  | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | alibi |
| d7c2b0eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | served_by | near | continuous | system_record | 228-01-2042-70110 | antenna | 1 |  |  |  | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 | CDR op-A |

(6 rows)

### Q3 - WHO WAS AT THE SCENE 02:00-03:10? Presence only: referenced places AND the negated alibi excluded by the model itself

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | entity_link | spatial_link | occupancy | attestation | bearer_entity | bearer_entity_type | is_presence | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 13794c16 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 5dc72691 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| c01515e1 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| d99b0e25 |  |  | none | device | unknown |  |  |  | epsg:4326 | self | at | once | instrument |  |  | 1 | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |

(4 rows)

### Q4 - ...and the REFERENCED pointers at the same place (leads, not presence)

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | entity_link | spatial_link | occupancy | attestation | bearer_entity | bearer_entity_type | is_presence | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | shared | references | once | system_record | scene-pin | poi | 0 | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |

(1 rows)

### Q5 - Duplicate evidence findings (never merged, always reported; duplicates SURFACE)

| finding | digest | copies | files |
|---|---|---|---|
| possible same evidence (file-independent) | 30f538506a79341f | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 389791817c58e048 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 3b95729fd5680e2c | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 555a6f030cbe56b5 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 6b28fad84ecd54a4 | 2 | /case/operatorA_cdr.csv | /case/operatorA_cdr_supplement.csv |
| possible same evidence (file-independent) | 7ea618480945eaf7 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | ef2dc130a610e92a | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| same file bytes | fp-cache-9f31 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |

(8 rows)

### Q6 - Convergence by normalized identifier: every mention of +41791234567, with its VERBATIM raw_identifier surfaced (same working id, different source text)

| identifier | raw_identifier | identifier_type | type | activity_role | source_label |
|---|---|---|---|---|---|
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41791234567 | msisdn | device | unknown | alibi |
| +41791234567 | +41791234567 | msisdn | device | sender | chat pin |

(6 rows)

### Q7 - Which identifiers used tower 228-01-2042-77341? (infrastructure archaeology; duplicates surface, no DISTINCT)

| entity | activity_role | at_local | source_label | input_file_path |
|---|---|---|---|---|
| +41791234567 | caller | 2026-03-14T02:20:31+01:00 | CDR op-A | /case/operatorA_cdr.csv |
| +41791234567 | caller | 2026-03-14T02:20:31+01:00 | CDR op-A | /case/operatorA_cdr_supplement.csv |

(2 rows)

### Q8 - Trace one CSV value back to the source (the 02:20 tower position), through the expression/component/integration/S2 chain

| method | latitude_wgs84 | longitude_wgs84 | s2_cell_id | s2_level | part | unit | source_field | verbatim | line_no | input_file_path |
|---|---|---|---|---|---|---|---|---|---|---|

(0 rows)

### Q9 - The two per-bound temporal projections: stated offset (L1) vs applied offset + local datetime_iso (L2), plus the ` N/A` assumed-UTC sentinel

| source_label | part | stated_offset | applied_lower | applied_upper | lower_datetime_iso | upper_datetime_iso |
|---|---|---|---|---|---|---|
| CDR op-A | lower | +01:00 | +1.0 | +1.0 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 |
| CDR op-A | lower | +01:00 | +1.0 | +1.0 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 |
| CDR op-A | lower | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | lower | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | duration | +01:00 | +1.0 | +1.0 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 |
| CDR op-A | duration | +01:00 | +1.0 | +1.0 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 |
| CDR op-A | duration | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | duration | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| chat pin | lower |  | N/A | N/A | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A |
| iOS cache | lower |  | N/A | N/A | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A |
| iOS cache | lower |  | N/A | N/A | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A |
| iOS cache | lower |  | N/A | N/A | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A |

(12 rows)

### Q10 - The analysis layer: the current ranker's verdict per observation (re-running a changed ranker ADDS rows, never mutates; the CSV projects the current method)

| method | record_rank_type | record_rank | entity_type | source_label | n |
|---|---|---|---|---|---|
| untangle/2.0 | anpr_read | 1 | vehicle | ANPR | 3 |
| untangle/2.0 | cdr_leg | 1 | device | CDR op-A | 4 |
| untangle/2.0 | gps_fix | 1 | device | iOS cache | 12 |

(3 rows)
