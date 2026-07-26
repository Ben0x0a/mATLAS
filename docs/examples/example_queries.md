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
at home during the robbery window (a `never_during_interval` assertion). The
cache.sqlite was ingested twice (folder + zip) and one CDR record appears in two
disclosures — both duplications are *findings*, never merges. The projection follows the
[../reference/csv_contract.md](../reference/csv_contract.md) column contract: `entity` is the
**normalised** identifier, `raw_identifier` the verbatim source text; `locates` is derived from
`entity_position_link`; the context columns (`counterpart`/`callee`/`imei`) are projected from
`activity.context_json` (only `assertion.activity_id`, which regroups the legs of one record, is
a useful projection of the activity); `t_lower`/`t_upper` are the offset-tagged local
`datetime_iso` (` N/A` = value, zone unknown, kept distinct from empty = no instant);
`s2_cell_id` is the signed-int64 S2 leaf cell.

Notable in the results:
- **Q1**: line = placement. Context parties are columns, not lines; the doubled iOS
  cache lines are the deliberate double-ingest (see Q5). No `SELECT DISTINCT` anywhere —
  duplicate evidence must SURFACE, never be silently collapsed in the projection.
- **Q3 vs Q4**: the model itself keeps the chat pin, the route search (referenced) and
  the negated alibi (`never_during_interval`) out of the "who was there" answer.
- **Q5**: both duplicate classes — same file bytes (folder==zip) and the
  file-independent `claim_digest` catching the overlapping CDR disclosure.
- **Q6**: convergence by the normalized `identifier` (`GROUP BY`, never a stored merge),
  with the verbatim `raw_identifier` surfaced beside it — the same working number written
  three different ways.
- **Q9**: the per-bound projection — the source's stated offset (L1) beside the
  applied offset + local `datetime_iso` (L2), and the ` N/A` assumed-UTC sentinel for
  the absolute-epoch and unix sources.


### Q1 - The flat projection (one line per placement); the whole example run

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | locates | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 64962225 | GE123456 | GE 123 456 | plate | vehicle | subject |  |  |  | cell_ref | infrastructure | 46.185 | 6.121 | 5155631170796163835 | 2026-03-12T17:22:05+01:00 | 2026-03-12T17:22:05+01:00 | ANPR |
| 0c8a843f | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | infrastructure | 46.228 | 6.152 | 5155606743496316837 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 | CDR op-A |
| 384f9922 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T19:00:00 N/A | iOS cache |
| c9f6c177 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T19:00:00 N/A | iOS cache |
| 14baf9de |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13T19:00:00 N/A | 2026-03-13T22:30:00 N/A | iOS visits |
| 2fb24ac8 | jdoe@gmail.com | jdoe@gmail.com | email | account | subject |  |  |  | address | referenced | 46.203 | 6.14 | 5155606543971413769 | 2026-03-13T21:04:09+00:00 | 2026-03-13T21:04:09+00:00 | nav search |
| f0432639 | jdoe@gmail.com | jdoe@gmail.com | email | account | subject |  |  |  | place_name | referenced |  |  |  | 2026-03-13T21:04:09+00:00 | 2026-03-13T21:04:09+00:00 | nav search |
| 00a7111e |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13T22:00:00 N/A | 2026-03-13T22:00:00 N/A | iOS cache |
| 8dbdec91 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13T22:00:00 N/A | 2026-03-13T22:00:00 N/A | iOS cache |
| 5c8cdc2e |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14T00:58:00 N/A | 2026-03-14T00:58:00 N/A | iOS cache |
| 8fc1b628 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14T00:58:00 N/A | 2026-03-14T00:58:00 N/A | iOS cache |
| 4ee39620 | GE123456 | GE 123 456 | plate | vehicle | subject |  |  |  | cell_ref | infrastructure | 46.2044 | 6.1432 | 5155606903156192425 | 2026-03-14T02:05:33+01:00 | 2026-03-14T02:05:33+01:00 | ANPR |
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | referenced | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |
| 0e0357eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 56456dbd | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| a82222a9 | +41762223344 | +41762223344 | msisdn | device | callee | +41791234567 |  |  | cell_ref | infrastructure | 46.1902 | 6.1265 | 5155631120092552241 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-B |
| 2de4a044 | Lobby-2 | Lobby-2 | camera | camera | subject |  |  |  | address | self | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T02:25:00+01:00 | 2026-03-14T02:50:00+01:00 | CCTV |
| 13794c16 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 5dc72691 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 17193524 | +41791234567 | +41791234567 | msisdn | device | subject |  |  |  | place_name | self | 46.193 | 6.129 | 5155631112766168683 | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | alibi |
| 3323cdf9 | GE-2026-04412 | GE-2026-04412 | case_ref | event | subject |  |  |  | address | self | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | police register |
| c01515e1 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| d99b0e25 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| 904ae0ca | GE123456 | GE 123 456 | plate | vehicle | subject |  |  |  | cell_ref | infrastructure | 46.2044 | 6.1432 | 5155606903156192425 | 2026-03-14T02:52:10+01:00 | 2026-03-14T02:52:10+01:00 | ANPR |
| 8576ae3f |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A | iOS cache |
| bd3b8052 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A | iOS cache |
| 5642495d |  | l'homme | none | person | reporter |  |  |  | place_name | self | 46.2102 | 6.1424 | 5155606893714199873 | 2026-03-14T11:00:00+01:00 | 2026-03-14T13:00:00+01:00 | audition |
| d7c2b0eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | infrastructure | 46.228 | 6.152 | 5155606743496316837 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 | CDR op-A |

(28 rows)

### Q2 - Timeline of ONE identifier across every source (convergence by GROUP-able normalized identifier, no stored merge)

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | locates | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0c8a843f | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | infrastructure | 46.228 | 6.152 | 5155606743496316837 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 | CDR op-A |
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | referenced | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |
| 0e0357eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 56456dbd | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 17193524 | +41791234567 | +41791234567 | msisdn | device | subject |  |  |  | place_name | self | 46.193 | 6.129 | 5155631112766168683 | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | alibi |
| d7c2b0eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +33612345678 | 352099001761481 | cell_ref | infrastructure | 46.228 | 6.152 | 5155606743496316837 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 | CDR op-A |

(6 rows)

### Q3 - WHO WAS AT THE SCENE 02:00-03:10? Presence only: referenced places AND the negated alibi excluded by the model itself

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | locates | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4ee39620 | GE123456 | GE 123 456 | plate | vehicle | subject |  |  |  | cell_ref | infrastructure | 46.2044 | 6.1432 | 5155606903156192425 | 2026-03-14T02:05:33+01:00 | 2026-03-14T02:05:33+01:00 | ANPR |
| 0e0357eb | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 56456dbd | +41791234567 | +41 79 123 45 67 | msisdn | device | caller |  | +41762223344 | 352099001761481 | cell_ref | infrastructure | 46.205 | 6.138 | 5155606547974423971 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 | CDR op-A |
| 2de4a044 | Lobby-2 | Lobby-2 | camera | camera | subject |  |  |  | address | self | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T02:25:00+01:00 | 2026-03-14T02:50:00+01:00 | CCTV |
| 13794c16 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 5dc72691 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A | iOS cache |
| 3323cdf9 | GE-2026-04412 | GE-2026-04412 | case_ref | event | subject |  |  |  | address | self | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T02:30:00+01:00 | 2026-03-14T02:45:00+01:00 | police register |
| c01515e1 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| d99b0e25 |  |  | none | device | subject |  |  |  | epsg:4326 | self | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A | iOS cache |
| 904ae0ca | GE123456 | GE 123 456 | plate | vehicle | subject |  |  |  | cell_ref | infrastructure | 46.2044 | 6.1432 | 5155606903156192425 | 2026-03-14T02:52:10+01:00 | 2026-03-14T02:52:10+01:00 | ANPR |

(10 rows)

### Q4 - ...and the REFERENCED pointers at the same place (leads, not presence)

| row_uid | entity | raw_identifier | identifier_type | entity_type | role | counterpart | callee | imei | spatial_referential | locates | lat | lon | s2_cell_id | t_lower | t_upper | source_label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 041178c2 | +41791234567 | +41791234567 | msisdn | device | sender |  |  |  | epsg:4326 | referenced | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A | chat pin |
| 2fb24ac8 | jdoe@gmail.com | jdoe@gmail.com | email | account | subject |  |  |  | address | referenced | 46.203 | 6.14 | 5155606543971413769 | 2026-03-13T21:04:09+00:00 | 2026-03-13T21:04:09+00:00 | nav search |

(2 rows)

### Q5 - Duplicate evidence findings (never merged, always reported; duplicates SURFACE)

| finding | digest | copies | files |
|---|---|---|---|
| possible same evidence (file-independent) | 09a62e2bd850d74b | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 84e86d48ec90c458 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | c2b2f213f630b93b | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | e3126b8b347de5fe | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | e4f5f722153ab426 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | e9e796c741c3a628 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | efc8304c073831b2 | 2 | /case/operatorA_cdr.csv | /case/operatorA_cdr_supplement.csv |
| same file bytes | fp-cache-9f31 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |

(8 rows)

### Q6 - Convergence by normalized identifier: every mention of +41791234567, with its VERBATIM raw_identifier surfaced (same working id, different source text)

| identifier | raw_identifier | identifier_type | type | entity_role | source_label |
|---|---|---|---|---|---|
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | caller | CDR op-A |
| +41791234567 | +41791234567 | msisdn | device | subject | alibi |
| +41791234567 | +41791234567 | msisdn | device | sender | chat pin |

(6 rows)

### Q7 - Which identifiers used tower 228-01-2042-77341? (infrastructure archaeology; duplicates surface, no DISTINCT)

| entity | entity_role | at_local | source_label | input_file_path |
|---|---|---|---|---|
| +41791234567 | caller | 2026-03-14T02:20:31+01:00 | CDR op-A | /case/operatorA_cdr.csv |
| +41791234567 | caller | 2026-03-14T02:20:31+01:00 | CDR op-A | /case/operatorA_cdr_supplement.csv |

(2 rows)

### Q8 - Trace one CSV value back to the source (the 02:20 tower position), through the expression/component/integration/S2 chain

| method | latitude_wgs84 | longitude_wgs84 | s2_cell_id | s2_level | part | spatial_role | source_field | verbatim | line_no | input_file_path |
|---|---|---|---|---|---|---|---|---|---|---|
| cell_register/2025-03 | 46.205 | 6.138 | 5155606547974423971 | 30 | raw_position | vicinity | cell | 228-01-2042-77341 | 2 | /case/operatorA_cdr.csv |

(1 rows)

### Q9 - The two per-bound temporal projections: stated offset (L1) vs applied offset + local datetime_iso (L2), plus the ` N/A` assumed-UTC sentinel

| source_label | part | temporal_role | stated_offset | applied_lower | applied_upper | lower_datetime_iso | upper_datetime_iso |
|---|---|---|---|---|---|---|---|
| CDR op-A | lower | start | +01:00 | +1.0 | +1.0 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 |
| CDR op-A | lower | start | +01:00 | +1.0 | +1.0 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 |
| CDR op-A | lower | start | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | lower | start | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | duration | end | +01:00 | +1.0 | +1.0 | 2026-03-13T18:40:12+01:00 | 2026-03-13T18:45:24+01:00 |
| CDR op-A | duration | end | +01:00 | +1.0 | +1.0 | 2026-03-14T11:05:00+01:00 | 2026-03-14T11:06:35+01:00 |
| CDR op-A | duration | end | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| CDR op-A | duration | end | +01:00 | +1.0 | +1.0 | 2026-03-14T02:20:31+01:00 | 2026-03-14T02:23:38+01:00 |
| chat pin | lower | start | N/A | N/A | N/A | 2026-03-14T01:11:20 N/A | 2026-03-14T01:11:20 N/A |
| iOS cache | lower | last_seen |  | N/A | N/A | 2026-03-14T02:00:00 N/A | 2026-03-14T02:00:00 N/A |
| iOS cache | lower | last_seen |  | N/A | N/A | 2026-03-14T01:25:00 N/A | 2026-03-14T01:25:00 N/A |
| iOS cache | lower | last_seen |  | N/A | N/A | 2026-03-14T01:40:00 N/A | 2026-03-14T01:40:00 N/A |

(12 rows)

### Q10 - The analysis layer: the current ranker's verdict per assertion (re-running a changed ranker ADDS rows, never mutates; the CSV projects the current method)

| method | record_type | record_rank | entity_type | source_label | n |
|---|---|---|---|---|---|
| untangle/2.0 | anpr_read | 1 | vehicle | ANPR | 3 |
| untangle/2.0 | cdr_leg | 1 | device | CDR op-A | 4 |
| untangle/2.0 | gps_fix | 1 | device | iOS cache | 12 |

(3 rows)
