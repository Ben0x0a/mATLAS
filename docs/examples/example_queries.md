# Example run database — worked queries against the model

A demonstration artefact showing real queries against the run database (see
[../model/schema.md](../model/schema.md)). Built by
[build_example_db.py](build_example_db.py) — deterministic, so rerunning it regenerates
[matlas_example.sqlite](matlas_example.sqlite) byte-for-byte apart from the
`case_metadata`/`run` uuid rows and the run-scoped metadata that carries the run id
(`run_archive`/`run_preset` links, `source.run_id`, `edit_log`). The DDL is hand-written here
for demonstration; the shipped schema is generated from the code.

The same build also writes [matlas_example.csv](matlas_example.csv) — the **flat CSV export
of this database**, so the output model can be read without running anything. It is produced
by the real projection (`core.flat_view.build_flat_view`) and the real writer
(`export.csv_flat.CsvFlatExporter._write`), so it is byte-for-byte what an operator gets:
28 rows, 104 columns — the 57 canonical columns of the
[CSV contract](../reference/csv_contract.md) followed by the 47 `orig_` verbatim
passthrough columns this fixture's sources carry. Database and CSV are always regenerated
together, so the two can never disagree.

**The story**: one night in Geneva. The suspect's phone (exhibit A) holds GPS fixes, a
home visit, a chat pin "meet me here" (scene coordinates, the app flagged it deleted)
and a route search to the scene address. Operator A's CDRs place the caller on the tower
next to the scene at 02:20 — operator B disclosed the same call (B-leg). ANPR read the
suspect's plate near the scene at 02:05 and 02:52. CCTV at the scene recorded
02:25-02:50. The robbery (police register) ran 02:30-02:45. A witness saw "l'homme" near
the station around noon, and an alibi statement claims the suspect's phone was **not**
at home during the robbery window (`temporal_link = never`, the only place a negation
can be stated). The
cache.sqlite was ingested twice (folder + zip) and one CDR record appears in two
disclosures — both duplications are *findings*, never merges. The projection follows the
[../reference/csv_contract.md](../reference/csv_contract.md) column contract: `entity` is the
**normalised** identifier of the TARGETED entity, `raw_identifier` the verbatim source text;
the `bearer_*` columns come from the SECOND entity join and carry the POSITION BEARER — the
antenna a phone was served by, the camera that saw a plate — which is why "which other
handsets hit this cell?" is now a `GROUP BY` rather than a string match. For a `subject`
row the bearer IS the targeted entity (decision 55), so the `bearer_*` columns repeat it;
`t_lower`/`t_upper` are RENDERED by the query
from the canonical UTC µs plus `offset_to_local_hours_*` — the database stores no local-clock
column, so a report always shows the zone configured now rather than one frozen at ingest;
`s2_cell_id` is the signed-int64 S2 leaf cell.

Notable in the results:
- **Q1**: line = placement. The bearer is a *column*, never a line — storage orders by whose
  position it is, the projection by who is under investigation. Context parties stay columns
  too; the doubled iOS cache lines are the deliberate double-ingest (see Q5). No
  `SELECT DISTINCT` anywhere — duplicate evidence must SURFACE, never be silently collapsed.
- **Q3 vs Q4**: the three links each answer one question (decision 55), so the filter
  states them one by one: a positive quantifier (`temporal_link IN (throughout,
  at_least_once)`) and a contact verb (`spatial_link IN (measured, …, located)`). The chat pin
  and the nav search (`shared` / `searched`) fail the verb; the alibi (`never`) fails the
  quantifier — negation lives on the temporal link alone, so it cannot be written twice.
  This is an explicit query, not the concomitance engine's presence predicate, which is
  still to be designed.
- **Q5**: both duplicate classes — same file bytes (folder==zip) and the file-independent
  `spatiotemporal_entity_hash` catching the overlapping CDR disclosure.
- **Q6**: convergence by the normalized `identifier` (`GROUP BY`, never a stored merge),
  with the verbatim `raw_identifier` surfaced beside it.
- **Q9**: the two temporal facts that must never be conflated — `stated_offset` (L1, the
  offset the SOURCE's clock carried, verbatim) beside `offset_to_local_hours_*` (L2, the
  offset the ANALYST adds to read the instant in the case's zone). Until 2026-09-07 one
  column tried to be both: it held the offset consumed to reach UTC, which is NULL for every
  epoch source, so on a real acquisition the configured zone reached the analyst on no row at
  all. Note every L2 value here is `+1.0` while the L1 values differ — that is the split
  working, not a bug.

*Note: the CDR and ANPR rows carry no coordinates in this build — the cell/camera register
resolution is a work-in-progress stub (`integrate/spatial.py`), so those bearers have an
identity but not yet a position. Q3 therefore returns only the GNSS-located rows.*

### Q1 - The flat projection (one line per placement); the whole example run

| row_uid_matlas | entity | raw_identifier | identifier_type | entity_type | entity_link_matlas | spatial_link_matlas | temporal_link_matlas | bearer_entity | bearer_entity_type | lat | lon | s2_cell_id_matlas | t_lower | t_upper | source_label_matlas |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 8ddf8192 | GE123456 | GE 123 456 | plate | vehicle | party | seen | at_least_once | CAM-B2-KM03 | camera |  |  |  | 2026-03-12 17:22:05+01:00 | 2026-03-12 17:22:05+01:00 | ANPR |
| c942d34a | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-70110 | antenna |  |  |  | 2026-03-13 18:40:12+01:00 | 2026-03-13 18:45:24+01:00 | CDR op-A |
| 28265d3d |  |  | none | device | subject | measured | at_least_once |  | device | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13 20:00:00+01:00 | 2026-03-13 20:00:00+01:00 | iOS cache |
| ebe80124 |  |  | none | device | subject | measured | at_least_once |  | device | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13 20:00:00+01:00 | 2026-03-13 20:00:00+01:00 | iOS cache |
| 43335066 |  |  | none | device | subject | measured | throughout |  | device | 46.193 | 6.129 | 5155631112766168683 | 2026-03-13 20:00:00+01:00 | 2026-03-13 23:30:00+01:00 | iOS visits |
| 3b9ae795 | jdoe@gmail.com | jdoe@gmail.com | email | account | party | searched | at_least_once | Rue du Stand 15, Geneve | poi |  |  |  | 2026-03-13 22:04:09+01:00 | 2026-03-13 22:04:09+01:00 | nav search |
| 9914d050 | jdoe@gmail.com | jdoe@gmail.com | email | account | party | searched | at_least_once | Home | poi |  |  |  | 2026-03-13 22:04:09+01:00 | 2026-03-13 22:04:09+01:00 | nav search |
| 71c4279e |  |  | none | device | subject | measured | at_least_once |  | device | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13 23:00:00+01:00 | 2026-03-13 23:00:00+01:00 | iOS cache |
| 78e6e57b |  |  | none | device | subject | measured | at_least_once |  | device | 46.1934 | 6.1288 | 5155631111126241589 | 2026-03-13 23:00:00+01:00 | 2026-03-13 23:00:00+01:00 | iOS cache |
| 5e49f7f5 |  |  | none | device | subject | measured | at_least_once |  | device | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14 01:58:00+01:00 | 2026-03-14 01:58:00+01:00 | iOS cache |
| afc55219 |  |  | none | device | subject | measured | at_least_once |  | device | 46.1988 | 6.1352 | 5155606538265142169 | 2026-03-14 01:58:00+01:00 | 2026-03-14 01:58:00+01:00 | iOS cache |
| 4b85c75f | GE123456 | GE 123 456 | plate | vehicle | party | seen | at_least_once | CAM-A7-KM12 | camera |  |  |  | 2026-03-14 02:05:33+01:00 | 2026-03-14 02:05:33+01:00 | ANPR |
| e6521dfc | +41791234567 | +41791234567 | msisdn | device | party | shared | at_least_once | scene-pin | poi | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14 02:11:20+01:00 | 2026-03-14 02:11:20+01:00 | chat pin |
| c59cf14b | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-77341 | antenna |  |  |  | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 | CDR op-A |
| cd46c9d6 | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-77341 | antenna |  |  |  | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 | CDR op-A |
| f06df094 | +41762223344 | +41762223344 | msisdn | device | party | served | at_least_once | 228-02-1011-55221 | antenna |  |  |  | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 | CDR op-B |
| a59fdcb4 | Lobby-2 | Lobby-2 | camera | camera | subject | located | throughout | Lobby-2 | camera |  |  |  | 2026-03-14 02:25:00+01:00 | 2026-03-14 02:50:00+01:00 | CCTV |
| 04db86dd |  |  | none | device | subject | measured | at_least_once |  | device | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14 02:25:00+01:00 | 2026-03-14 02:25:00+01:00 | iOS cache |
| 6512173f |  |  | none | device | subject | measured | at_least_once |  | device | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14 02:25:00+01:00 | 2026-03-14 02:25:00+01:00 | iOS cache |
| cebcf0c9 | +41791234567 | +41791234567 | msisdn | device | party | located | never | Home | poi |  |  |  | 2026-03-14 02:30:00+01:00 | 2026-03-14 02:45:00+01:00 | alibi |
| 0d124914 | GE-2026-04412 | GE-2026-04412 | case_ref | event | subject | located | throughout | GE-2026-04412 | event |  |  |  | 2026-03-14 02:30:00+01:00 | 2026-03-14 02:45:00+01:00 | police register |
| 231d7f9f |  |  | none | device | subject | measured | at_least_once |  | device | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14 02:40:00+01:00 | 2026-03-14 02:40:00+01:00 | iOS cache |
| e3b22774 |  |  | none | device | subject | measured | at_least_once |  | device | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14 02:40:00+01:00 | 2026-03-14 02:40:00+01:00 | iOS cache |
| 3d06170d | GE123456 | GE 123 456 | plate | vehicle | party | seen | at_least_once | CAM-A7-KM12 | camera |  |  |  | 2026-03-14 02:52:10+01:00 | 2026-03-14 02:52:10+01:00 | ANPR |
| 33414655 |  |  | none | device | subject | measured | at_least_once |  | device | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14 03:00:00+01:00 | 2026-03-14 03:00:00+01:00 | iOS cache |
| 592888c1 |  |  | none | device | subject | measured | at_least_once |  | device | 46.1975 | 6.133 | 5155631105453945475 | 2026-03-14 03:00:00+01:00 | 2026-03-14 03:00:00+01:00 | iOS cache |
| d861246c |  | l'homme | none | person | subject | located | at_least_once |  | person |  |  |  | 2026-03-14 11:00:00+01:00 | 2026-03-14 13:00:00+01:00 | audition |
| e15cd55a | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-70110 | antenna |  |  |  | 2026-03-14 11:05:00+01:00 | 2026-03-14 11:06:35+01:00 | CDR op-A |

(28 rows)

### Q2 - Timeline of ONE identifier across every source (convergence by GROUP-able normalized identifier, no stored merge)

| row_uid_matlas | entity | raw_identifier | identifier_type | entity_type | entity_link_matlas | spatial_link_matlas | temporal_link_matlas | bearer_entity | bearer_entity_type | lat | lon | s2_cell_id_matlas | t_lower | t_upper | source_label_matlas |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c942d34a | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-70110 | antenna |  |  |  | 2026-03-13 18:40:12+01:00 | 2026-03-13 18:45:24+01:00 | CDR op-A |
| e6521dfc | +41791234567 | +41791234567 | msisdn | device | party | shared | at_least_once | scene-pin | poi | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14 02:11:20+01:00 | 2026-03-14 02:11:20+01:00 | chat pin |
| c59cf14b | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-77341 | antenna |  |  |  | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 | CDR op-A |
| cd46c9d6 | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-77341 | antenna |  |  |  | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 | CDR op-A |
| cebcf0c9 | +41791234567 | +41791234567 | msisdn | device | party | located | never | Home | poi |  |  |  | 2026-03-14 02:30:00+01:00 | 2026-03-14 02:45:00+01:00 | alibi |
| e15cd55a | +41791234567 | +41 79 123 45 67 | msisdn | device | party | served | at_least_once | 228-01-2042-70110 | antenna |  |  |  | 2026-03-14 11:05:00+01:00 | 2026-03-14 11:06:35+01:00 | CDR op-A |

(6 rows)

### Q3 - WHO WAS AT THE SCENE 02:00-03:10? Positive claims with a contact verb only: searched/shared/cached places AND the negated alibi excluded. (An explicit filter, not the engine's presence predicate, which decision 55 left to design.)

| row_uid_matlas | entity | raw_identifier | identifier_type | entity_type | entity_link_matlas | spatial_link_matlas | temporal_link_matlas | bearer_entity | bearer_entity_type | lat | lon | s2_cell_id_matlas | t_lower | t_upper | source_label_matlas |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 04db86dd |  |  | none | device | subject | measured | at_least_once |  | device | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14 02:25:00+01:00 | 2026-03-14 02:25:00+01:00 | iOS cache |
| 6512173f |  |  | none | device | subject | measured | at_least_once |  | device | 46.2024 | 6.1403 | 5155606543654947399 | 2026-03-14 02:25:00+01:00 | 2026-03-14 02:25:00+01:00 | iOS cache |
| 231d7f9f |  |  | none | device | subject | measured | at_least_once |  | device | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14 02:40:00+01:00 | 2026-03-14 02:40:00+01:00 | iOS cache |
| e3b22774 |  |  | none | device | subject | measured | at_least_once |  | device | 46.2028 | 6.1401 | 5155606543821444557 | 2026-03-14 02:40:00+01:00 | 2026-03-14 02:40:00+01:00 | iOS cache |

(4 rows)

### Q4 - ...and the places only searched, shared or cached at the same spot (leads, not presence)

| row_uid_matlas | entity | raw_identifier | identifier_type | entity_type | entity_link_matlas | spatial_link_matlas | temporal_link_matlas | bearer_entity | bearer_entity_type | lat | lon | s2_cell_id_matlas | t_lower | t_upper | source_label_matlas |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| e6521dfc | +41791234567 | +41791234567 | msisdn | device | party | shared | at_least_once | scene-pin | poi | 46.203 | 6.14 | 5155606543971413769 | 2026-03-14 02:11:20+01:00 | 2026-03-14 02:11:20+01:00 | chat pin |

(1 rows)

### Q5 - Duplicate evidence findings (never merged, always reported; duplicates SURFACE)

| finding | digest | copies | files |
|---|---|---|---|
| possible same evidence (file-independent) | 00d762182bb39669 | 2 | /case/operatorA_cdr.csv | /case/operatorA_cdr_supplement.csv |
| possible same evidence (file-independent) | 2a43ffbfa43014e8 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 3d4bab18e66f8b11 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 54da738277a2f958 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | 6392070d3d7728cd | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | b6488fecf581fef1 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| possible same evidence (file-independent) | e9e31a1d6fd0db1f | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |
| same file bytes | fp-cache-9f31 | 2 | /case/exhibit_A/cache.sqlite | /case/exhibit_A_copy.zip |

(8 rows)

### Q6 - Convergence by normalized identifier: every mention of +41791234567, with its VERBATIM raw_identifier surfaced (same working id, different source text)

| identifier | raw_identifier | identifier_type | type | source_label |
|---|---|---|---|---|
| +41791234567 | +41 79 123 45 67 | msisdn | device | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | CDR op-A |
| +41791234567 | +41 79 123 45 67 | msisdn | device | CDR op-A |
| +41791234567 | +41791234567 | msisdn | device | alibi |
| +41791234567 | +41791234567 | msisdn | device | chat pin |

(6 rows)

### Q7 - Which identifiers used tower 228-01-2042-77341? (infrastructure archaeology; duplicates surface, no DISTINCT)

| entity | at_local | source_label | input_file_path |
|---|---|---|---|
| +41791234567 | 2026-03-14 02:20:31+01:00 | CDR op-A | /case/operatorA_cdr.csv |
| +41791234567 | 2026-03-14 02:20:31+01:00 | CDR op-A | /case/operatorA_cdr_supplement.csv |

(2 rows)

### Q8 - Trace one CSV value back to the source (the 02:20 tower position), through the expression/component/integration/S2 chain

| method | latitude_wgs84 | longitude_wgs84 | s2_cell_id | s2_level | part | unit | source_field | verbatim | line_no | input_file_path |
|---|---|---|---|---|---|---|---|---|---|---|

(0 rows)

### Q9 - The two temporal facts side by side: the offset the SOURCE stated (L1, verbatim) vs the offset to the CASE's local zone (L2) and the clock it renders

| source_label | part | stated_offset | lower_unix_utc_us | offset_to_local_hours_lower | offset_to_local_hours_upper | lower_local | upper_local |
|---|---|---|---|---|---|---|---|
| CDR op-A | lower | +01:00 | 1773451231000000 | +1.0 | +1.0 | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 |
| CDR op-A | lower | +01:00 | 1773451231000000 | +1.0 | +1.0 | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 |
| CDR op-A | lower | +01:00 | 1773482700000000 | +1.0 | +1.0 | 2026-03-14 11:05:00+01:00 | 2026-03-14 11:06:35+01:00 |
| CDR op-A | lower | +01:00 | 1773423612000000 | +1.0 | +1.0 | 2026-03-13 18:40:12+01:00 | 2026-03-13 18:45:24+01:00 |
| CDR op-A | duration | +01:00 | 1773451231000000 | +1.0 | +1.0 | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 |
| CDR op-A | duration | +01:00 | 1773451231000000 | +1.0 | +1.0 | 2026-03-14 02:20:31+01:00 | 2026-03-14 02:23:38+01:00 |
| CDR op-A | duration | +01:00 | 1773482700000000 | +1.0 | +1.0 | 2026-03-14 11:05:00+01:00 | 2026-03-14 11:06:35+01:00 |
| CDR op-A | duration | +01:00 | 1773423612000000 | +1.0 | +1.0 | 2026-03-13 18:40:12+01:00 | 2026-03-13 18:45:24+01:00 |
| chat pin | lower |  | 1773450680000000 | +1.0 | +1.0 | 2026-03-14 02:11:20+01:00 | 2026-03-14 02:11:20+01:00 |
| iOS cache | lower |  | 1773452400000000 | +1.0 | +1.0 | 2026-03-14 02:40:00+01:00 | 2026-03-14 02:40:00+01:00 |
| iOS cache | lower |  | 1773428400000000 | +1.0 | +1.0 | 2026-03-13 20:00:00+01:00 | 2026-03-13 20:00:00+01:00 |
| iOS cache | lower |  | 1773453600000000 | +1.0 | +1.0 | 2026-03-14 03:00:00+01:00 | 2026-03-14 03:00:00+01:00 |

(12 rows)

### Q10 - The analysis layer: the current ranker's verdict per observation (re-running a changed ranker ADDS rows, never mutates; the CSV projects the current method)

| method | record_rank | record_order | entity_type | source_label | n |
|---|---|---|---|---|---|
| untangle/2.0 | 1 | main | vehicle | ANPR | 3 |
| untangle/2.0 | 1 | main | device | CDR op-A | 4 |
| untangle/2.0 | 1 | main | device | iOS cache | 12 |

(3 rows)
