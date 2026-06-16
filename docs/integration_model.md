# Integration Model

The canonical output model is a flat table. One output row represents one
spatio-temporal assertion:

```text
Entity X had relation Y to position P during temporal interval T.
```

Every temporal assertion is modeled as an interval. An instant is an interval
whose lower and upper bounds are identical.

## Columns

| Column | Type | Unit | Example | Description |
| --- | --- | --- | --- | --- |
| `Entity` | string | none | `device-123` | Main entity concerned by the assertion: device, account, person, vehicle, etc. |
| `Entity type` | string | none | `device` | Type/category of `Entity`. |
| `Linked Entity` | string or null | none | `account-42` | Optional secondary entity linked to the assertion. |
| `Timestamp interval lower bound original` | source scalar or null | source-defined | `724369200.0` | Original source value for the earliest temporal bound of the assertion. |
| `Timestamp interval lower bound type` | string | none | `FIRST_SEEN` | Source column name that supplied the lower temporal bound. |
| `Timestamp interval lower bound UNIX ms` | integer or null | milliseconds since Unix epoch | `1704067200000` | Lower temporal bound normalized to Unix milliseconds. |
| `Timestamp interval upper bound original` | source scalar or null | source-defined | `724369500.0` | Original source value for the latest temporal bound. Same as lower bound for an instant. |
| `Timestamp interval upper bound type` | string | none | `LAST_SEEN` | Source column name that supplied the upper temporal bound. Same as lower-bound type for an instant. |
| `Timestamp interval upper bound UNIX ms` | integer or null | milliseconds since Unix epoch | `1704067500000` | Upper temporal bound normalized to Unix milliseconds. Same as lower bound for an instant. |
| `Timestamp accuracy` | integer, float, or null | milliseconds | `1000` | Temporal uncertainty or resolution of the timestamp assertion when known. |
| `raw_timestamp` | string, source scalar, or null | source-defined | `tomorrow` | Raw temporal expression or source value preserved when the timestamp is interpreted, parsed, normalized, or otherwise encoded into interval bounds. |
| `temporal_source` | string or null | none | `NTP` | Source or mechanism behind the temporal value when known, such as `NTP`, `internal_clock`, `server_clock`, `file_metadata`, or a tool/source-specific label. |
| `Temporal relation` | string | controlled value | `continuous_during_interval` | How the assertion applies across the interval. See [Temporal Relations](#temporal-relations). |
| `Latitude` | float or null | decimal degrees | `46.2044` | Latitude of the asserted position. |
| `Longitude` | float or null | decimal degrees | `6.1432` | Longitude of the asserted position. |
| `Altitude` | float or null | meters | `410.5` | Altitude of the asserted position when known. |
| `Position type` | string | none | `origin` | Source-derived position type. The mapper derives common words from latitude/longitude column names, falling back to `latitude_column|longitude_column`. |
| `raw_position` | string, source scalar, or null | source-defined | `I'm at home` | Raw spatial expression or source value preserved when the position is geocoded, inferred, normalized, or encoded into latitude/longitude. Examples include a place label, address text, WiFi BSSID, cell identifier, or message body fragment. |
| `position_source` | string or null | none | `GNSS` | Source or mechanism behind the asserted position when known, such as `GNSS`, `WiFi`, `cell`, `fused`, `manual`, or a tool/source-specific label. |
| `Source file` | string | none | `Cache.sqlite::table=ZRTCLLOCATIONMO` | Logical source element processed: CSV file, Excel sheet, SQLite table/query, etc. |
| `Source original path` | string | none | `/private/var/mobile/.../Cache.sqlite` | Original filesystem path or internal evidence path when known. |
| `Source raw data` | string, source scalar, or null | source-defined | `row-42` | Optional source-specific raw-data reference, path, identifier, or column value. |
| `Tool label` | string or null | none | `Cached Locations` | Label from source context, such as filename, sheet name, table name, or configured value. |
| `Record type` | string or null | controlled value | `main` | Untangle result. Usually `main` or `additional`; empty if untangle did not rank the row. |
| `Record rank` | integer or null | rank | `1` | Untangle rank within comparable assertions; empty if not ranked. |
| `Horizontal accuracy` | float or null | meters | `12.4` | Spatial horizontal uncertainty. |
| `Vertical Accuracy` | float or null | meters | `6.0` | Spatial vertical uncertainty. |
| `Horizontal Speed` | float or null | meters per second | `1.8` | Horizontal speed. |
| `Vertical Speed` | float or null | meters per second | `0.1` | Vertical speed. |
| `Horizontal speed accuracy` | float or null | meters per second | `0.5` | Uncertainty for horizontal speed. |
| `Vertical speed accuracy` | float or null | meters per second | `0.2` | Uncertainty for vertical speed. |
| `Entity-position link` | string or null | none | `observed` | Evaluation relation between entity and position. This is not a grouping key. |
| `Entity-Timestamp link` | string or null | none | `recorded_at` | Evaluation relation between entity and timestamp interval. This is not a grouping key. |
| `spatial-temporal link` | string or null | none | `continuous_presence` | Evaluation relation between spatial and temporal dimensions. This is not a grouping key. |
| `details` | JSON string or absent | none | `{"source_row": 12}` | Preserved unmapped source columns and parser details when `details_mode=json`. With `details_mode=append_column`, details are emitted as `details_<source column>` columns instead. |

## Populated Row Examples

These examples show model coverage, not preset mapping logic. They use
compact row tables: omitted canonical fields are `null` unless the example
states otherwise.

### Coverage Overview

| Evidence type | Computation-ready position | Raw position preserved | Time model | Main uncertainty signal |
| --- | --- | --- | --- | --- |
| GNSS coordinate | Direct latitude/longitude/altitude | Source coordinate payload | Instant | GNSS horizontal/vertical accuracy |
| Text message | Geocoded semantic place | Message text such as `I'm at home tomorrow` | Interpreted interval | Temporal phrase width and geocode radius |
| WiFi BSSID | Access-point geolocation | BSSID and SSID | Instant scan time | Access-point geolocation radius |
| Cellular network | Cell-sector approximate coordinate | MCC/MNC/LAC/CID/sector | Connection interval | Cell sector radius |
| IP address | IP geolocation coordinate | IP address | Server log instant | IP geolocation radius |
| Street address | Geocoded address coordinate | Address string | Validity interval | Geocoder/address confidence radius |
| Geohash | Geohash cell center | Geohash string | Event instant | Geohash cell precision |

### GNSS Coordinate

A device location sample with direct GNSS coordinates. The computation-ready
position is the coordinate pair; the raw source value is still preserved.

| Field | Value |
| --- | --- |
| `Entity` | `device-123` |
| `Entity type` | `device` |
| `Linked Entity` | `account-42` |
| `Timestamp interval lower bound original` | `2026-05-01T12:00:14.250Z` |
| `Timestamp interval lower bound type` | `RECORDED_AT` |
| `Timestamp interval lower bound UNIX ms` | `1777636814250` |
| `Timestamp interval upper bound original` | `2026-05-01T12:00:14.250Z` |
| `Timestamp interval upper bound type` | `RECORDED_AT` |
| `Timestamp interval upper bound UNIX ms` | `1777636814250` |
| `Timestamp accuracy` | `250` |
| `raw_timestamp` | `2026-05-01T12:00:14.250Z` |
| `temporal_source` | `GNSS_receiver_clock` |
| `Temporal relation` | `instant` |
| `Latitude` | `46.204391` |
| `Longitude` | `6.143158` |
| `Altitude` | `410.5` |
| `Position type` | `sample` |
| `raw_position` | `lat=46.204391; lon=6.143158; provider=gps` |
| `position_source` | `GNSS` |
| `Horizontal accuracy` | `4.8` |
| `Vertical Accuracy` | `8.2` |
| `Horizontal Speed` | `1.4` |
| `Vertical Speed` | `null` |
| `Entity-position link` | `observed_device_position` |
| `Entity-Timestamp link` | `recorded_at` |
| `spatial-temporal link` | `same_sample` |
| `Source file` | `Cache.sqlite::table=ZRTCLLOCATIONMO` |
| `Source original path` | `/private/var/mobile/Library/Caches/com.apple.routined/Cache.sqlite` |
| `Tool label` | `ZRTCLLOCATIONMO` |

### Text Message With Natural Language

A message says "I'm at home tomorrow". The normalized interval and position are
derived, but the raw text remains available for review.

| Field | Value |
| --- | --- |
| `Entity` | `account-alice` |
| `Entity type` | `account` |
| `Linked Entity` | `device-123` |
| `Timestamp interval lower bound original` | `tomorrow` |
| `Timestamp interval lower bound type` | `MESSAGE_TEXT_TIME` |
| `Timestamp interval lower bound UNIX ms` | `1777680000000` |
| `Timestamp interval upper bound original` | `tomorrow` |
| `Timestamp interval upper bound type` | `MESSAGE_TEXT_TIME` |
| `Timestamp interval upper bound UNIX ms` | `1777766399999` |
| `Timestamp accuracy` | `86400000` |
| `raw_timestamp` | `tomorrow` |
| `temporal_source` | `natural_language` |
| `Temporal relation` | `unknown_during_interval` |
| `Latitude` | `46.20195` |
| `Longitude` | `6.14501` |
| `Position type` | `semantic_place` |
| `raw_position` | `I'm at home tomorrow` |
| `position_source` | `semantic_geocode` |
| `Horizontal accuracy` | `150` |
| `Entity-position link` | `claimed_by_message` |
| `Entity-Timestamp link` | `claimed_by_message` |
| `spatial-temporal link` | `text_interpretation` |
| `Source file` | `Messages.csv` |
| `Source original path` | `/private/var/mobile/Library/SMS/sms.db` |
| `Source raw data` | `message_row_id=8812` |
| `Tool label` | `Messages` |
| `details` | `{"message_body":"I'm at home tomorrow"}` |

### WiFi BSSID Position

A WiFi observation can be encoded as an approximate position while preserving
the BSSID that produced the spatial inference.

| Field | Value |
| --- | --- |
| `Entity` | `device-123` |
| `Entity type` | `device` |
| `Timestamp interval lower bound original` | `2026-05-01T12:03:09Z` |
| `Timestamp interval lower bound type` | `SCAN_TIME` |
| `Timestamp interval lower bound UNIX ms` | `1777636989000` |
| `Timestamp interval upper bound original` | `2026-05-01T12:03:09Z` |
| `Timestamp interval upper bound type` | `SCAN_TIME` |
| `Timestamp interval upper bound UNIX ms` | `1777636989000` |
| `Timestamp accuracy` | `1000` |
| `raw_timestamp` | `2026-05-01T12:03:09Z` |
| `temporal_source` | `internal_clock` |
| `Temporal relation` | `instant` |
| `Latitude` | `46.20418` |
| `Longitude` | `6.14342` |
| `Position type` | `access_point_geolocation` |
| `raw_position` | `BSSID=44:65:0d:12:34:56; SSID=ExampleCafe` |
| `position_source` | `WiFi` |
| `Horizontal accuracy` | `35` |
| `Entity-position link` | `near_observed_radio` |
| `Entity-Timestamp link` | `scan_time` |
| `spatial-temporal link` | `wifi_scan` |
| `Source file` | `Wi-Fi Discovered Devices.csv` |
| `Source raw data` | `artifact_id=wifi-204` |
| `Tool label` | `Wi-Fi Discovered Devices` |

### Cellular Network Position

A cellular record is often much less precise. The model keeps the tower/sector
identifier and expresses uncertainty through horizontal accuracy.

| Field | Value |
| --- | --- |
| `Entity` | `device-123` |
| `Entity type` | `device` |
| `Timestamp interval lower bound original` | `2026-05-01T12:05:00Z` |
| `Timestamp interval lower bound type` | `CONNECTED_FROM` |
| `Timestamp interval lower bound UNIX ms` | `1777637100000` |
| `Timestamp interval upper bound original` | `2026-05-01T12:18:00Z` |
| `Timestamp interval upper bound type` | `CONNECTED_TO` |
| `Timestamp interval upper bound UNIX ms` | `1777637880000` |
| `Timestamp accuracy` | `1000` |
| `raw_timestamp` | `2026-05-01T12:05:00Z/2026-05-01T12:18:00Z` |
| `temporal_source` | `network_event_time` |
| `Temporal relation` | `continuous_during_interval` |
| `Latitude` | `46.2102` |
| `Longitude` | `6.1368` |
| `Position type` | `cell_sector` |
| `raw_position` | `MCC=228; MNC=01; LAC=4242; CID=982341; sector=2` |
| `position_source` | `cellular` |
| `Horizontal accuracy` | `1500` |
| `Entity-position link` | `connected_to_cell` |
| `Entity-Timestamp link` | `network_connection_interval` |
| `spatial-temporal link` | `cell_attachment` |
| `Source file` | `Cellular Network.csv` |
| `Tool label` | `Cellular Network` |

### IP Address Geolocation

IP geolocation can be represented, but it should normally carry broad spatial
uncertainty and a clear raw position value.

| Field | Value |
| --- | --- |
| `Entity` | `account-alice` |
| `Entity type` | `account` |
| `Linked Entity` | `session-abc` |
| `Timestamp interval lower bound original` | `2026-05-01T12:30:44Z` |
| `Timestamp interval lower bound type` | `LOGIN_TIME` |
| `Timestamp interval lower bound UNIX ms` | `1777638644000` |
| `Timestamp interval upper bound original` | `2026-05-01T12:30:44Z` |
| `Timestamp interval upper bound type` | `LOGIN_TIME` |
| `Timestamp interval upper bound UNIX ms` | `1777638644000` |
| `Timestamp accuracy` | `1000` |
| `raw_timestamp` | `2026-05-01T12:30:44Z` |
| `temporal_source` | `server_clock` |
| `Temporal relation` | `instant` |
| `Latitude` | `46.2044` |
| `Longitude` | `6.1432` |
| `Position type` | `ip_geolocation` |
| `raw_position` | `203.0.113.42` |
| `position_source` | `IP_geolocation` |
| `Horizontal accuracy` | `25000` |
| `Entity-position link` | `session_source_ip` |
| `Entity-Timestamp link` | `login_time` |
| `spatial-temporal link` | `server_log_event` |
| `Source file` | `Authentication Logs.csv` |
| `Source raw data` | `log_line=552019` |
| `Tool label` | `Authentication Logs` |

### Street Address

An address can be geocoded into coordinates while the original address remains
auditable.

| Field | Value |
| --- | --- |
| `Entity` | `person-alice` |
| `Entity type` | `person` |
| `Timestamp interval lower bound original` | `2026-05-01` |
| `Timestamp interval lower bound type` | `PROFILE_VALID_FROM` |
| `Timestamp interval lower bound UNIX ms` | `1777593600000` |
| `Timestamp interval upper bound original` | `2026-06-01` |
| `Timestamp interval upper bound type` | `PROFILE_VALID_TO` |
| `Timestamp interval upper bound UNIX ms` | `1780272000000` |
| `Timestamp accuracy` | `86400000` |
| `raw_timestamp` | `valid during May 2026` |
| `temporal_source` | `profile_validity` |
| `Temporal relation` | `continuous_during_interval` |
| `Latitude` | `37.33182` |
| `Longitude` | `-122.03118` |
| `Position type` | `address` |
| `raw_position` | `1 Infinite Loop, Cupertino, CA 95014, USA` |
| `position_source` | `street_address_geocode` |
| `Horizontal accuracy` | `30` |
| `Entity-position link` | `declared_address` |
| `Entity-Timestamp link` | `profile_validity_interval` |
| `spatial-temporal link` | `declared_continuous_address` |
| `Source file` | `Contacts.xlsx::sheet=Profiles` |
| `Tool label` | `Profiles` |

### Geohash Coordinate

A geohash is already spatially encoded but still has a raw representation that
should be preserved.

| Field | Value |
| --- | --- |
| `Entity` | `vehicle-17` |
| `Entity type` | `vehicle` |
| `Timestamp interval lower bound original` | `1777639000000` |
| `Timestamp interval lower bound type` | `EVENT_UNIX_MS` |
| `Timestamp interval lower bound UNIX ms` | `1777639000000` |
| `Timestamp interval upper bound original` | `1777639000000` |
| `Timestamp interval upper bound type` | `EVENT_UNIX_MS` |
| `Timestamp interval upper bound UNIX ms` | `1777639000000` |
| `Timestamp accuracy` | `1000` |
| `raw_timestamp` | `1777639000000` |
| `temporal_source` | `device_event_clock` |
| `Temporal relation` | `instant` |
| `Latitude` | `46.20443` |
| `Longitude` | `6.14323` |
| `Position type` | `geohash_cell_center` |
| `raw_position` | `u0hq2t8` |
| `position_source` | `geohash` |
| `Horizontal accuracy` | `19` |
| `Entity-position link` | `reported_vehicle_position` |
| `Entity-Timestamp link` | `event_time` |
| `spatial-temporal link` | `same_event` |
| `Source file` | `Vehicle Events.csv` |
| `Tool label` | `Vehicle Events` |

## Temporal Relations

| Value | Meaning | Computation impact |
| --- | --- | --- |
| `instant` | The assertion applies at one timestamp. Lower bound equals upper bound. | Compare as a zero-duration interval; delta-time rules can still match nearby instants. |
| `once_during_interval` | The assertion happened exactly once somewhere inside the interval. | Temporal overlap gives weak/possible support unless the interval is narrow. |
| `sporadic_during_interval` | The assertion happened at least once, possibly multiple times, but not continuously. | Temporal overlap gives possible support. |
| `continuous_during_interval` | The assertion applies for the full interval. | Temporal overlap gives strong support. |
| `never_during_interval` | The assertion is explicitly false for the full interval. | Can be used as exclusion or contradiction evidence. |
| `unknown_during_interval` | The source gives an interval but does not define whether the assertion is once, sporadic, continuous, or never. | Temporal overlap gives low-confidence possible support. |

## Computation Notes

Meeting or co-location detection should use:

- `Timestamp interval lower bound UNIX ms`
- `Timestamp interval upper bound UNIX ms`
- `Timestamp accuracy`
- `raw_timestamp`
- `temporal_source`
- `Latitude`
- `Longitude`
- `Horizontal accuracy`
- `raw_position`
- `position_source`
- `Temporal relation`

Two instants can match when they are within the configured temporal and spatial
deltas. For example, positions five minutes apart and fifty meters apart match
when `delta_time >= 5 minutes` and `delta_space >= 50 meters`.

For intervals, temporal distance is zero when intervals overlap; otherwise it is
the smallest gap between bounds. Spatial distance should account for available
spatial accuracy when the analysis method supports it.

## Replaced Fields

The interval model replaces the previous single timestamp fields:

- `Timestamp original`
- `Timestamp type`
- `Timestamp standard UNIX ms`

Those values are now represented by lower/upper bound fields. For an instant,
lower and upper bound values are identical.
