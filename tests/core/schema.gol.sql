CREATE TABLE matlas_meta (
    schema_version TEXT,
    families_version TEXT,
    normalisation_ruleset TEXT,
    stemming_ruleset TEXT
);

CREATE TABLE case_metadata (
    case_id TEXT PRIMARY KEY,
    case_number TEXT NOT NULL,
    title TEXT,
    description TEXT,
    created_at TEXT
);

CREATE TABLE run (
    run_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES case_metadata(case_id),
    input_path TEXT,
    started_at TEXT,
    finished_at TEXT,
    tool_version TEXT,
    settings_json TEXT,
    examiner TEXT
);

CREATE TABLE archive (
    archive_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    cd_sha256 TEXT NOT NULL
);

CREATE INDEX ix_archive_cd ON archive(cd_sha256);

CREATE TABLE run_archive (
    run_archive_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    archive_id TEXT NOT NULL REFERENCES archive(archive_id),
    parsed_at TEXT,
    skipped INTEGER NOT NULL,
    UNIQUE (run_id, archive_id)
);

CREATE TABLE source (
    source_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    input_file_path TEXT NOT NULL,
    inner_path TEXT,
    data_source TEXT,
    format TEXT,
    subunit TEXT,
    fingerprint_sha256 TEXT,
    sibling_fingerprints_json TEXT,
    exhibit_ref TEXT,
    source_tier TEXT CHECK (source_tier IN ('primary', 'secondary', 'unknown') OR source_tier IS NULL),
    record_count INTEGER,
    UNIQUE (run_id, input_file_path, inner_path, subunit)
);

CREATE INDEX ix_source_fp ON source(fingerprint_sha256);

CREATE TABLE preset (
    preset_id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    name TEXT,
    version TEXT,
    definition_digest TEXT NOT NULL,
    UNIQUE (key, version)
);

CREATE TABLE run_preset (
    run_preset_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    UNIQUE (run_id, preset_id)
);

CREATE TABLE raw_record (
    raw_record_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source(source_id),
    line_no INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    native_record_uid TEXT,
    recovery_state TEXT CHECK (recovery_state IN ('live', 'intact', 'deleted', 'recovered', 'unknown') OR recovery_state IS NULL),
    UNIQUE (source_id, line_no)
);

CREATE TABLE activity (
    activity_id TEXT PRIMARY KEY,
    kind TEXT CHECK (kind IN ('call', 'message', 'trip', 'transaction', 'stay', 'observation', 'unknown') OR kind IS NULL),
    context_json TEXT
);

CREATE TABLE activity_record (
    activity_record_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL REFERENCES activity(activity_id),
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    UNIQUE (activity_id, raw_record_id)
);

CREATE TABLE entity (
    entity_id TEXT PRIMARY KEY,
    identifier TEXT,
    raw_identifier TEXT,
    identifier_type TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('device', 'person', 'vehicle', 'account', 'camera', 'antenna', 'case', 'label', 'event', 'poi', 'unknown') OR type IS NULL),
    concerns TEXT,
    concerns_origin TEXT CHECK (concerns_origin IN ('preset_mapped', 'operator_param') OR concerns_origin IS NULL),
    concerns_uid TEXT
);

CREATE INDEX ix_entity_norm ON entity(identifier_type, identifier);

CREATE INDEX ix_entity_concerns ON entity(concerns_uid);

CREATE TABLE spatial_expression (
    spatial_expression_id TEXT PRIMARY KEY,
    referential TEXT NOT NULL CHECK (referential IN ('epsg:4326', 'geohash', 'mgrs', 'address', 'place_name', 'cell_ref', 'asset_ref', 'region', 'unknown') OR referential IS NULL),
    spatial_source TEXT
);

CREATE TABLE spatial_component (
    spatial_component_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL REFERENCES spatial_expression(spatial_expression_id),
    ordinal INTEGER NOT NULL,
    part TEXT NOT NULL,
    spatial_role TEXT CHECK (spatial_role IN ('vicinity', 'entry', 'exit', 'origin', 'destination', 'waypoint', 'unknown') OR spatial_role IS NULL),
    source_field TEXT,
    raw_value TEXT,
    UNIQUE (spatial_expression_id, ordinal)
);

CREATE TABLE temporal_expression (
    temporal_expression_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('instant', 'interval', 'open_lower', 'open_upper') OR kind IS NULL),
    referential TEXT CHECK (referential IN ('unix_epoch_s', 'unix_epoch_ms', 'unix_epoch_us', 'apple_cfabsolute', 'webkit', 'filetime', 'excel_serial', 'iso8601', 'local_string', 'unknown') OR referential IS NULL),
    temporal_source TEXT
);

CREATE TABLE temporal_component (
    temporal_component_id TEXT PRIMARY KEY,
    temporal_expression_id TEXT NOT NULL REFERENCES temporal_expression(temporal_expression_id),
    ordinal INTEGER NOT NULL,
    part TEXT NOT NULL,
    temporal_role TEXT CHECK (temporal_role IN ('start', 'end', 'booking', 'check_in', 'check_out', 'last_seen', 'unknown') OR temporal_role IS NULL),
    stated_offset TEXT,
    source_field TEXT,
    raw_value TEXT,
    UNIQUE (temporal_expression_id, ordinal)
);

CREATE TABLE assertion (
    assertion_id TEXT PRIMARY KEY,
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    output_ordinal INTEGER NOT NULL,
    entity_id TEXT NOT NULL UNIQUE REFERENCES entity(entity_id),
    content_hash TEXT NOT NULL,
    claim_digest TEXT NOT NULL,
    entity_origin TEXT CHECK (entity_origin IN ('preset_mapped', 'operator_param') OR entity_origin IS NULL),
    activity_id TEXT REFERENCES activity(activity_id),
    spatial_expression_id TEXT UNIQUE REFERENCES spatial_expression(spatial_expression_id),
    temporal_expression_id TEXT UNIQUE REFERENCES temporal_expression(temporal_expression_id),
    entity_role TEXT CHECK (entity_role IN ('subject', 'caller', 'callee', 'sender', 'recipient', 'reporter', 'owner', 'unknown') OR entity_role IS NULL),
    entity_position_link TEXT CHECK (entity_position_link IN ('at', 'within_range_of', 'at_fixed_detector', 'references', 'claimed_at', 'inferred', 'unknown') OR entity_position_link IS NULL),
    entity_time_link TEXT CHECK (entity_time_link IN ('observed_at', 'event_at', 'recorded_at', 'reported_for', 'intended_for', 'never_during_interval', 'unknown') OR entity_time_link IS NULL),
    spatial_temporal_link TEXT CHECK (spatial_temporal_link IN ('instant', 'continuous_during_interval', 'once_during_interval', 'sporadic_during_interval', 'never_during_interval', 'unknown_during_interval') OR spatial_temporal_link IS NULL),
    deleted TEXT CHECK (deleted IN ('live', 'intact', 'deleted', 'recovered', 'unknown') OR deleted IS NULL),
    multiplicity INTEGER,
    source_label TEXT,
    input_record_id TEXT,
    UNIQUE (raw_record_id, preset_id, output_ordinal)
);

CREATE INDEX ix_assertion_ch ON assertion(content_hash);

CREATE INDEX ix_assertion_cd ON assertion(claim_digest);

CREATE INDEX ix_assertion_act ON assertion(activity_id);

CREATE TABLE spatial_integration (
    spatial_integration_id TEXT PRIMARY KEY,
    spatial_expression_id TEXT NOT NULL UNIQUE REFERENCES spatial_expression(spatial_expression_id),
    method TEXT NOT NULL,
    latitude_wgs84 REAL,
    longitude_wgs84 REAL,
    altitude_m REAL,
    horizontal_accuracy_m REAL,
    vertical_accuracy_m REAL,
    horizontal_speed_kmh REAL,
    vertical_speed_kmh REAL,
    horizontal_speed_accuracy_kmh REAL,
    vertical_speed_accuracy_kmh REAL,
    heading_deg REAL,
    heading_accuracy_deg REAL,
    beam_azimuth_deg REAL,
    beam_width_deg REAL
);

CREATE TABLE spatial_index (
    spatial_index_id TEXT PRIMARY KEY,
    spatial_integration_id TEXT NOT NULL UNIQUE REFERENCES spatial_integration(spatial_integration_id),
    s2_cell_id INTEGER,
    s2_level INTEGER
);

CREATE INDEX ix_spatial_index_cell ON spatial_index(s2_cell_id);

CREATE TABLE timespan_integration (
    timespan_integration_id TEXT PRIMARY KEY,
    temporal_expression_id TEXT NOT NULL UNIQUE REFERENCES temporal_expression(temporal_expression_id),
    method TEXT NOT NULL,
    lower_unix_utc_us INTEGER,
    upper_unix_utc_us INTEGER,
    lower_datetime_iso TEXT,
    upper_datetime_iso TEXT,
    lower_date INTEGER,
    upper_date INTEGER,
    lower_time INTEGER,
    upper_time INTEGER,
    utc_offset_hours_lower TEXT,
    utc_offset_hours_upper TEXT,
    time_accuracy_us INTEGER
);

CREATE TABLE analysis (
    analysis_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL REFERENCES assertion(assertion_id),
    method TEXT NOT NULL,
    record_type TEXT,
    record_rank INTEGER,
    UNIQUE (assertion_id, method)
);

CREATE TABLE edit_log (
    edit_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    table_name TEXT,
    row_id TEXT,
    column TEXT,
    old_value TEXT,
    new_value TEXT,
    edit_kind TEXT CHECK (edit_kind IN ('update', 'new_row', 'empty_row') OR edit_kind IS NULL),
    edited_by TEXT,
    edited_at TEXT
);

CREATE VIEW duplicate_sources AS
    SELECT fingerprint_sha256, COUNT(*) AS copies,
           GROUP_CONCAT(input_file_path, ' | ') AS files
    FROM source GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;

CREATE VIEW possible_same_evidence AS
    SELECT a.claim_digest, COUNT(*) AS copies,
           GROUP_CONCAT(s.input_file_path, ' | ') AS files
    FROM assertion a
    JOIN raw_record r ON r.raw_record_id = a.raw_record_id
    JOIN source s ON s.source_id = r.source_id
    GROUP BY a.claim_digest HAVING COUNT(*) > 1;
