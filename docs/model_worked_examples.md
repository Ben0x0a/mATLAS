# Worked examples — nine source types through the model

Status: **validation annex, regenerated 2026-07-14** (task
[../tasks/T00.md](../tasks/T00.md)) against the 19-table schema
([run_db_schema.md](run_db_schema.md), decisions 16-36). Each example shows what one
real record becomes under the settled model: the rows per layer, the flat CSV line(s),
and what it exercises. The rows below are transcribed from
[examples/matlas_example.sqlite](examples/matlas_example.sqlite) (built by
[examples/build_example_db.py](examples/build_example_db.py)); ids are omitted for
readability and can be recovered by running the builder.

What changed from the decision-14 draft this file used to carry:

- **`assertion_mention` is gone (D18).** A record's other parties are no longer mention
  rows. Activity-scoped context (callee, the caller's own IMEI, recipients) is
  preset-mapped into `activity.context_json` (canonical JSON, **inside** the L1 digest
  projection); a detector/camera *is* the spatial expression (its id is the
  `cell_ref`/`address` component); everything else stays verbatim in `raw_json`.
- **The activity is a grouping bonus (D35).** It only ties the legs of one record
  together; everything exploitable reads from entity/links/roles (a leg's counterpart
  is the other assertion sharing its `activity_id`). The CSV projects `activity_id`
  (to regroup legs); kind/context columns are optional bonus. An event from a
  structured list IS the entity (`type=event`, identifier == concerns, D36; a placed
  `poi` works the same way).
- **The entity is one lean row (D17/D21).** `identifier` is normalised at ingest
  (E.164, lowercase email, plate without spaces; `NULL` for a purely typed mention like
  a `device`); `raw_identifier` is the source's own text, verbatim; `identifier_type`
  names the normalisation; `type` is what the thing IS. The attributed party is
  `concerns` (absorbs the old `linked_entity`), with a derived `concerns_uid`.
- **Roles + stated offsets live on the components (D25).** Each bound carries its own
  `temporal_role` (a visit's lower is `check_in`, its upper `check_out`) and its own
  `stated_offset`; each value-group its own `spatial_role`.
- **Two derived layers, method-stamped.** `spatial_integration` (+ an S2 leaf cell in
  `spatial_index`, D31) and `timespan_integration` (with the D32 per-bound
  `datetime_iso`/`date`/`time`; ` N/A` = value, zone unknown; empty = no instant).
- The analyst graph (person chains, mention resolution, cross-record "same call") is
  **deferred out of v1 (D16)** — absent from every walk below.

**Line counts at a glance**: ANPR 1 · CCTV 1 · witness 1 · CDR 1 (2 iff the B-leg has
its own cell — never 3) · Cache.sqlite 1 · visit 1 · robbery 1 · route search 2 ·
shared pin 1.

**Reading note (D15)**: where a walk lists several `part=… raw=…` pairs on one
expression, those are physically `spatial_component` / `temporal_component` rows (one
per contributing source field: a lat+lon pair is two rows, a date+time bound two rows,
a CDR start+duration two rows) — kept inline here for readability.

---

## 1 · ANPR read — **1 line**

**The record** (`anpr_export.csv`, line 1):
`{"camera_id":"CAM-A7-KM12","ocr_conf":"0.92","plate":"GE 123 456","ts":"2026-03-14 02:05:33"}`

```
L0  source anpr_export.csv (fingerprint fp-anpr-44dd) · raw_record line 1, raw_json verbatim
L1  assertion  entity_role=subject · at_fixed_detector · observed_at · instant · preset=anpr_ch
    entity     identifier="GE123456"  raw_identifier="GE 123 456"  identifier_type=plate  type=vehicle
    spatial    referential=cell_ref  spatial_source=camera
               component  part=raw_position  spatial_role=vicinity  source_field=camera_id  raw="CAM-A7-KM12"
    temporal   instant  referential=local_string  temporal_source=NTP
               component  part=lower  temporal_role=last_seen  stated_offset="+01:00"  raw="2026-03-14 02:05:33"
L2  spatial_integration  method=camera_register/2025-11  lat=46.2044 lon=6.1432
    spatial_index         s2_cell_id=5155606903156192425  s2_level=30
    timespan_integration  method=tz_stated_offset/1.0  lower_datetime_iso="2026-03-14T02:05:33+01:00"
                          utc_offset_hours_lower=+1.0
```

**CSV**: **one line** — the plate at the camera's position (`locates=infrastructure`).
The camera is *how* the plate was placed: it IS the `cell_ref` spatial expression, no
detector row of its own (D18). `ocr_conf` survives in `orig_ocr_conf`.

---

## 2 · CCTV footage (DVR export index) — **1 line**

**The record** (`dvr_index.csv`, line 1):
`{"channel":"Lobby-2","end":"2026-03-14 02:50:00","site":"Rue du Stand 15, 1204 Geneve","start":"2026-03-14 02:25:00"}`

```
L1  assertion  entity_role=subject · at · recorded_at · continuous_during_interval · preset=dvr_generic
    entity     identifier="Lobby-2"  raw_identifier="Lobby-2"  identifier_type=camera  type=camera
    spatial    referential=address  spatial_source=manual
               component  part=raw_position  spatial_role=vicinity  source_field=site  raw="Rue du Stand 15, 1204 Geneve"
    temporal   interval  referential=local_string  temporal_source=internal_clock
               component  part=lower  role=start  stated_offset=(none)  raw="2026-03-14 02:25:00"
               component  part=upper  role=end    stated_offset=(none)  raw="2026-03-14 02:50:00"
L2  spatial_integration  method=geocode_nominatim/2026-06  lat=46.203 lon=6.14  s2_cell=5155606543971413769
    timespan_integration method=tz_localize/tzdb2026a  lower="2026-03-14T02:25:00+01:00" upper="…02:50:00+01:00"
                         applied +1.0/+1.0  (the source stated no offset; the tz-db supplied it)
```

**CSV**: one line — the camera recording at that address during the interval. When a
*person* is later identified on the footage, that is a deferred-analysis act (D16),
never an ingest line.

---

## 3 · Witness audition — **1 line**

**The record** (`audition_pv.csv`, line 1):
`{"statement":"J'ai vu l'homme pres de la gare vers midi le 14 mars","witness":"Marie Dupont"}`

```
L1  assertion  entity_role=reporter · claimed_at · reported_for · continuous_during_interval · preset=audition
    entity     identifier=NULL  raw_identifier="l'homme"  identifier_type=none  type=person
               concerns="Marie Dupont"  concerns_origin=preset_mapped   (the witness, fixed at parsing)
    spatial    referential=place_name  spatial_source=manual
               component  part=raw_position  spatial_role=vicinity  source_field=statement  raw="pres de la gare"
    temporal   interval  referential=local_string
               component  part=lower  role=start  stated_offset="+01:00"  raw="vers midi le 14 mars"
               component  part=upper  role=end    stated_offset="+01:00"  raw="vers midi le 14 mars"
L2  spatial_integration  method=place_centroid/geocoder-x (R14: substitution visible)  lat=46.2102 lon=6.1424
    timespan_integration method=fuzzy_time/1.0  lower="…11:00:00+01:00" upper="…13:00:00+01:00"  time_accuracy_us=3.6e9
```

**CSV**: **one line** — "l'homme" near the station around noon; the links say
claimed/reported (testimony, not measurement). The witness has no placement of her own,
so she is the `concerns` of the mention, not a line. `identifier=NULL` (a descriptive
label is not canonicalisable), so nothing can ever converge on it.

---

## 4 · CDR — **1 line** (2 iff the B-leg cell is present; never 3)

**The record** (`operatorA_cdr.csv`, line 2 — the 02:20 call near the scene):
`{"a_party":"+41 79 123 45 67","b_party":"+41 76 222 33 44","cell":"228-01-2042-77341","duration":"187","imei":"352099001761481","start":"2026-03-14 02:20:31"}`

```
L1  activity   kind=call  context_json={"callee":"+41762223344","caller_imei":"352099001761481"}
               activity_record link → this record (exactly one at ingest, D26)
    assertion  entity_role=caller · within_range_of · event_at · continuous_during_interval · preset=cdr_generic
    entity     identifier="+41791234567"  raw_identifier="+41 79 123 45 67"  identifier_type=msisdn  type=device
    spatial    referential=cell_ref  spatial_source=cell
               component  part=raw_position  spatial_role=vicinity  source_field=cell  raw="228-01-2042-77341"
    temporal   interval  referential=local_string  temporal_source=NTP
               component  part=lower     role=start  stated_offset="+01:00"  raw="2026-03-14 02:20:31"
               component  part=duration  role=end    stated_offset="+01:00"  raw="187"   (upper computed)
L2  spatial_integration  method=cell_register/2025-03  lat=46.205 lon=6.138  beam_azimuth=120 beam_width=65
                         s2_cell_id=5155606547974423971
    timespan_integration method=tz_localize/tzdb2026a+start_plus_duration/1.0
                         lower="2026-03-14T02:20:31+01:00" upper="2026-03-14T02:23:38+01:00"  applied +1.0/+1.0
```

**CSV**: **one line** — the caller at the tower sector; the projection lifts the callee
into a `callee` column and the caller's own IMEI into an `imei` column, both from
`context_json` (D18). The B-leg disclosure (example below) yields a *second* line on a
separate record — never three, because the IMEI is a facet of the caller, not a party.

### 4b · operator B disclosure of the SAME call (the B-leg) — a second line

**The record** (`operatorB_cdr.csv`, line 1):
`{"callee":"+41762223344","caller":"+41791234567","cell_b":"228-02-1011-55221","duration":"187","start":"2026-03-14 02:20:31"}`

```
L1  activity   kind=call  context_json={"counterpart":"+41791234567"}
    assertion  entity_role=callee · within_range_of · event_at · continuous_during_interval · preset=cdr_generic
    entity     identifier="+41762223344"  raw_identifier="+41762223344"  identifier_type=msisdn  type=device
    spatial    referential=cell_ref  spatial_source=cell
               component  part=raw_position  spatial_role=vicinity  source_field=cell_b  raw="228-02-1011-55221"
L2  spatial_integration  method=cell_register/2025-03  lat=46.1902 lon=6.1265  s2_cell=5155631120092552241
```

Two operators disclosing one call are two records → two activities → **two lines**, each
leg at *its own* tower. v1 makes no automatic "same call" link (deferred, D16); the
overlap surfaces instead through the claim/content digests where the records coincide.

---

## 5 · iOS Cache.sqlite fix — **1 line**

**The record** (`cache.sqlite`, line 4 — the 02:25 fix near the scene):
`{"ZCOURSE":"15","ZHORIZONTALACCURACY":"18","ZLATITUDE":"46.202400","ZLONGITUDE":"6.140300","ZSPEED":"1.1","ZTIMESTAMP":"763712700"}`

```
L1  assertion  entity_role=subject · at · observed_at · instant · preset=ios_cache
    entity     identifier=NULL  raw_identifier=NULL  identifier_type=none  type=device
    spatial    referential=epsg:4326  spatial_source=GNSS
               component  part=latitude_wgs84         source_field=ZLATITUDE           raw="46.202400"
               component  part=longitude_wgs84        source_field=ZLONGITUDE          raw="6.140300"
               component  part=horizontal_accuracy_m  source_field=ZHORIZONTALACCURACY raw="18"
               component  part=horizontal_speed_kmh   source_field=ZSPEED              raw="1.1"   (m/s, verbatim)
               component  part=heading_deg            source_field=ZCOURSE             raw="15"
    temporal   instant  referential=apple_cfabsolute  temporal_source=internal_clock
               component  part=lower  temporal_role=last_seen  stated_offset=(none)  raw="763712700"
L2  spatial_integration  method=passthrough_wgs84/1.0+unit_convert/1.0  lat=46.2024 lon=6.1403
                         horizontal_accuracy_m=18.0  horizontal_speed_kmh=3.96 (m/s→km/h)  heading_deg=15.0
                         s2_cell_id=5155606543654947399
    timespan_integration method=epoch_cfabsolute/1.0  lower_datetime_iso="2026-03-14T01:25:00 N/A"
                         utc_offset_hours_lower="N/A"  (absolute epoch → no zone; the LOUD sentinel)
```

**CSV**: one line. Another phone's cache produces a different entity row —
`identifier=NULL` and `type=device`, so nothing exists to merge two devices (§2d stays
closed). Note the two distinct time facts: the source stated *no* offset
(`stated_offset` empty) yet the tool *assumed* UTC (`applied="N/A"`, ` N/A` on the
`datetime_iso`) — "value, zone unknown" is kept visibly distinct from "no value".

---

## 6 · Visit with entry & exit timestamps — **1 line**

**The record** (`local.sqlite`, line 1):
`{"ZENTRYDATE":"763689600","ZEXITDATE":"763702200","ZLAT":"46.193000","ZLON":"6.129000"}`

```
L1  assertion  entity_role=subject · at · observed_at · continuous_during_interval · preset=ios_visits
    entity     identifier=NULL  identifier_type=none  type=device
    spatial    referential=epsg:4326  spatial_source=GNSS
               component  part=latitude_wgs84   source_field=ZLAT  raw="46.193000"
               component  part=longitude_wgs84  source_field=ZLON  raw="6.129000"
    temporal   interval  referential=apple_cfabsolute
               component  part=lower  temporal_role=check_in   source_field=ZENTRYDATE raw="763689600"
               component  part=upper  temporal_role=check_out  source_field=ZEXITDATE  raw="763702200"
L2  spatial_integration  method=passthrough_wgs84/1.0  lat=46.193 lon=6.129  s2_cell=5155631112766168683
    timespan_integration method=epoch_cfabsolute/1.0  lower="2026-03-13T19:00:00 N/A" upper="…22:30:00 N/A"
```

**CSV**: one line — a presence at the place; the entry/exit provenance survives per
bound (`check_in`/`check_out` roles + `source_field` `ZENTRYDATE`/`ZEXITDATE`). A DST
change mid-visit would be carried by the two applied-offset columns (D9/D32).

---

## 7 · Robbery in a police register — **1 line**

**The record** (`police_register.csv`, line 1):
`{"case":"GE-2026-04412","from":"2026-03-14 02:30","location":"Rue du Stand 15, Geneve","offense":"robbery","to":"2026-03-14 02:45","victim":"M. Keller"}`

```
L1  assertion  entity_role=subject · claimed_at · reported_for · continuous_during_interval · preset=police_reg
    entity     identifier="GE-2026-04412"  raw_identifier="GE-2026-04412"  identifier_type=case_ref  type=event
               concerns="GE-2026-04412"  concerns_origin=preset_mapped   (identifier == concerns — D36)
    spatial    referential=address  spatial_source=manual
               component  part=raw_position  spatial_role=vicinity  source_field=location  raw="Rue du Stand 15, Geneve"
    temporal   interval  referential=local_string
               component  part=lower  role=start  raw="2026-03-14 02:30"
               component  part=upper  role=end    raw="2026-03-14 02:45"
L2  spatial_integration  method=geocode_nominatim/2026-06  lat=46.203 lon=6.14  s2_cell=5155606543971413769
    timespan_integration method=tz_localize/tzdb2026a  lower="…02:30:00+01:00" upper="…02:45:00+01:00"
```

**CSV**: **one line** — the event itself, placed (D36: an event from a structured list
IS the entity; a placed `poi` works the same way). The links say claimed/reported (a
register, not a sensor). The victim and the offence stay verbatim in raw_json → orig_;
no activity row — a single placement has nothing to group (D35). `suspect=unknown` was
never in this record, so the model fabricates nothing.

---

## 8 · Route navigation search — **2 lines** (two places, legitimately)

**The record** (`maps_history.db`, line 1):
`{"account":"jdoe@gmail.com","destination":"Rue du Stand 15, Geneve","origin":"Home","query_ts":"2026-03-13T21:04:09Z"}`

```
L1  activity   kind=trip  context_json={"destination":"Rue du Stand 15, Geneve","origin":"Home"}
    assertion ord0  entity_role=subject · references · event_at · instant · preset=nav_generic
      entity   identifier="jdoe@gmail.com"  identifier_type=email  type=account
      spatial  referential=address  component part=raw_position  spatial_role=destination  raw="Rue du Stand 15, Geneve"
      → spatial_integration geocode_nominatim/2026-06  lat=46.203 lon=6.14  s2_cell=5155606543971413769
    assertion ord1  entity_role=subject · references · event_at · instant · preset=nav_generic
      entity   identifier="jdoe@gmail.com"  identifier_type=email  type=account
      spatial  referential=place_name  component part=raw_position  spatial_role=origin  raw="Home"
      → spatial_integration NONE ("Home" ungeocodable) → empty coordinates by construction
    temporal (both)  instant  referential=iso8601  component part=lower stated_offset="+00:00" raw="2026-03-13T21:04:09Z"
                     → passthrough_utc/1.0  "2026-03-13T21:04:09+00:00"
```

**CSV**: **two lines** — the one case where two lines are *correct*: the record
references **two distinct places** (`spatial_role` origin, destination). Both are
`locates=referenced` — coordinates present (or empty), presence never claimed.

---

## 9 · Position shared in instant messaging — **1 line**

**The record** (`chat.db`, line 1):
`{"caption":"meet me here","is_deleted":"1","lat":"46.203000","lon":"6.140000","recipient":"+41762223344","sender":"+41791234567","ts":"1773450680"}`

```
L0  raw_record.recovery_state=live   (how the bytes were obtained)
L1  activity   kind=message  context_json={"recipient":"+41762223344"}
    assertion  entity_role=sender · references · event_at · instant · deleted=deleted · preset=im_generic
               (the APP's own flag — a different fact from recovery_state: R11)
    entity     identifier="+41791234567"  raw_identifier="+41791234567"  identifier_type=msisdn  type=device
    spatial    referential=epsg:4326  spatial_source=GNSS
               component part=latitude_wgs84  source_field=lat raw="46.203000"
               component part=longitude_wgs84 source_field=lon raw="6.140000"
    temporal   instant  referential=unix_epoch_s  component part=lower raw="1773450680"
L2  spatial_integration  method=passthrough_wgs84/1.0  lat=46.203 lon=6.14  s2_cell=5155606543971413769
    timespan_integration method=epoch_unix_s/1.0  lower_datetime_iso="2026-03-14T01:11:20 N/A"  applied="N/A"
```

**CSV**: **one line** — sender with the pin's coordinates but `locates=referenced`
("meet me here" ≠ "I am here"); the recipient is a `context` column from
`context_json`. A 20-recipient group message is still **one line** with a recipients
list in `context_json`.

---

## What the exercise surfaced

- **D18** (this revision): context parties live in `raw_json` + `activity.context_json`
  + the spatial expression itself — no `assertion_mention`. The line count *is* the
  placement count. ANPR/CCTV/witness/robbery/pin = 1; CDR = 1 or 2, never 3.
- **D17/D21**: the entity is one lean row — `identifier` (normalised) beside
  `raw_identifier` (verbatim); convergence is `GROUP BY (identifier_type, identifier)`,
  never a stored merge; `type=device` with `identifier=NULL` keeps §2d closed.
- **D25**: each bound carries its own role + stated offset (visit → check_in/check_out;
  route → origin/destination).
- **D31/D32**: every point gets an S2 leaf cell; every bound gets an offset-tagged
  local `datetime_iso`, with the ` N/A` sentinel distinguishing "zone unknown" (cache,
  visit, chat pin, absolute/epoch sources) from "no instant" (empty).
- **Negation**: an `alibi_statement.csv` record adds a `never_during_interval`
  assertion ("the suspect's phone was NOT at home during the robbery window"), which
  presence queries exclude — the v1 stand-in for the deferred graph layer.
- Round-trip verified in all nine: every CSV value traces to a raw field or a named
  method; deleting L2+L3 loses no evidence.
