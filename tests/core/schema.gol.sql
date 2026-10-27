CREATE TABLE matlas_meta (
    schema_version TEXT,
    families_version TEXT,
    normalisation_ruleset TEXT,
    stemming_ruleset TEXT
);

CREATE TABLE model_description (
    table_name TEXT NOT NULL,
    field_name TEXT,
    type TEXT,
    unit TEXT,
    matlas_enrichment INTEGER,
    description TEXT NOT NULL,
    UNIQUE (table_name, field_name)
);

CREATE TABLE case_metadata (
    case_id TEXT PRIMARY KEY NOT NULL,
    case_number TEXT NOT NULL,
    title TEXT,
    description TEXT,
    created_at TEXT,
    created_by TEXT
);

CREATE TABLE run (
    run_id TEXT PRIMARY KEY NOT NULL,
    case_id TEXT NOT NULL REFERENCES case_metadata(case_id),
    input_path TEXT,
    started_at TEXT,
    finished_at TEXT,
    tool_version TEXT,
    settings_json TEXT,
    examiner TEXT
);

CREATE TABLE archive (
    archive_id TEXT PRIMARY KEY NOT NULL,
    path TEXT NOT NULL UNIQUE,
    cd_sha256 TEXT NOT NULL
);

CREATE INDEX ix_archive_cd ON archive(cd_sha256);

CREATE TABLE run_archive (
    run_archive_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    archive_id TEXT NOT NULL REFERENCES archive(archive_id),
    parsed_at TEXT,
    skipped INTEGER NOT NULL,
    UNIQUE (run_id, archive_id)
);

CREATE TABLE source_file (
    source_file_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    input_file_path TEXT NOT NULL,
    inner_path TEXT,
    reported_data_source TEXT,
    format TEXT,
    subunit TEXT,
    fingerprint_sha256 TEXT,
    sibling_fingerprints_json TEXT,
    exhibit_ref TEXT,
    source_tier TEXT CHECK (source_tier IN ('primary', 'secondary', 'unknown') OR source_tier IS NULL),
    record_count INTEGER,
    UNIQUE (run_id, input_file_path, inner_path, subunit)
);

CREATE INDEX ix_source_fp ON source_file(fingerprint_sha256);

CREATE TABLE preset (
    preset_id TEXT PRIMARY KEY NOT NULL,
    key TEXT NOT NULL,
    name TEXT,
    version TEXT,
    definition_digest TEXT NOT NULL,
    UNIQUE (key, version)
);

CREATE TABLE run_preset (
    run_preset_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES run(run_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    UNIQUE (run_id, preset_id)
);

CREATE TABLE raw_record (
    raw_record_id TEXT PRIMARY KEY NOT NULL,
    source_file_id TEXT NOT NULL REFERENCES source_file(source_file_id),
    line_no INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    native_record_uid TEXT,
    recovery_state TEXT CHECK (recovery_state IN ('live', 'recovered') OR recovery_state IS NULL),
    recovery_source TEXT CHECK (recovery_source IN ('wal', 'journal', 'freelist', 'freespace', 'unknown') OR recovery_source IS NULL),
    UNIQUE (source_file_id, line_no)
);

CREATE TABLE activity (
    activity_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT CHECK (kind IN ('call', 'message', 'trip', 'transaction', 'stay', 'observation', 'unknown') OR kind IS NULL)
);

CREATE TABLE entity (
    entity_id TEXT PRIMARY KEY NOT NULL,
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
    spatial_expression_id TEXT PRIMARY KEY NOT NULL,
    position_type TEXT NOT NULL CHECK (position_type IN ('coordinate', 'grid_reference', 'street_address', 'cell_id', 'bssid', 'unknown') OR position_type IS NULL),
    referential TEXT CHECK (referential IN ('epsg:4326', 'geohash', 'mgrs', 'plus_code', 'unknown') OR referential IS NULL),
    spatial_source TEXT
);

CREATE TABLE spatial_component (
    spatial_component_id TEXT PRIMARY KEY NOT NULL,
    spatial_expression_id TEXT NOT NULL REFERENCES spatial_expression(spatial_expression_id),
    ordinal INTEGER NOT NULL,
    part TEXT NOT NULL,
    source_field TEXT,
    raw_value TEXT,
    unit TEXT,
    UNIQUE (spatial_expression_id, ordinal)
);

CREATE TABLE temporal_expression (
    temporal_expression_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('instant', 'interval', 'open_lower', 'open_upper') OR kind IS NULL),
    referential TEXT CHECK (referential IN ('unix_epoch_s', 'unix_epoch_ms', 'unix_epoch_us', 'apple_cfabsolute', 'webkit', 'filetime', 'excel_serial', 'iso8601', 'local_string', 'unknown') OR referential IS NULL),
    temporal_source TEXT
);

CREATE TABLE temporal_component (
    temporal_component_id TEXT PRIMARY KEY NOT NULL,
    temporal_expression_id TEXT NOT NULL REFERENCES temporal_expression(temporal_expression_id),
    ordinal INTEGER NOT NULL,
    part TEXT NOT NULL,
    stated_offset TEXT,
    source_field TEXT,
    raw_value TEXT,
    UNIQUE (temporal_expression_id, ordinal)
);

CREATE TABLE observation (
    observation_id TEXT PRIMARY KEY NOT NULL,
    raw_record_id TEXT NOT NULL REFERENCES raw_record(raw_record_id),
    preset_id TEXT NOT NULL REFERENCES preset(preset_id),
    entity_id TEXT NOT NULL UNIQUE REFERENCES entity(entity_id),
    observation_content_hash TEXT NOT NULL,
    spatiotemporal_entity_hash TEXT NOT NULL,
    bearer_entity_id TEXT UNIQUE REFERENCES entity(entity_id),
    activity_id TEXT REFERENCES activity(activity_id),
    spatial_expression_id TEXT UNIQUE REFERENCES spatial_expression(spatial_expression_id),
    temporal_expression_id TEXT UNIQUE REFERENCES temporal_expression(temporal_expression_id),
    entity_link TEXT CHECK (entity_link IN ('subject', 'party', 'mentioned', 'unknown') OR entity_link IS NULL),
    spatial_link TEXT CHECK (spatial_link IN ('measured', 'registered', 'served', 'seen', 'used', 'stayed', 'located', 'searched', 'shared', 'cached', 'unknown') OR spatial_link IS NULL),
    temporal_link TEXT CHECK (temporal_link IN ('throughout', 'at_least_once', 'never', 'not_throughout', 'unknown') OR temporal_link IS NULL),
    recovery_state TEXT CHECK (recovery_state IN ('live', 'recovered') OR recovery_state IS NULL),
    recovery_source TEXT CHECK (recovery_source IN ('wal', 'journal', 'freelist', 'freespace', 'unknown') OR recovery_source IS NULL),
    source_label TEXT,
    input_record_id TEXT,
    source_record_uid TEXT,
    CHECK ((entity_link IS 'subject') = (bearer_entity_id IS entity_id)),
    CHECK (entity_link IS NOT 'party' OR (bearer_entity_id IS NOT NULL AND bearer_entity_id IS NOT entity_id)),
    CHECK (entity_link IS NOT 'unknown' OR bearer_entity_id IS NULL),
    CHECK (spatial_expression_id IS NOT NULL OR spatial_link IS NULL)
);

CREATE INDEX ix_observation_ch ON observation(observation_content_hash);

CREATE INDEX ix_observation_cd ON observation(spatiotemporal_entity_hash);

CREATE INDEX ix_observation_act ON observation(activity_id);

CREATE TABLE spatial_integration (
    spatial_integration_id TEXT PRIMARY KEY NOT NULL,
    spatial_expression_id TEXT NOT NULL UNIQUE REFERENCES spatial_expression(spatial_expression_id),
    method TEXT NOT NULL,
    position_raw TEXT,
    referential TEXT,
    spatial_source TEXT,
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
    spatial_index_id TEXT PRIMARY KEY NOT NULL,
    spatial_integration_id TEXT NOT NULL UNIQUE REFERENCES spatial_integration(spatial_integration_id),
    s2_cell_id INTEGER,
    s2_level INTEGER
);

CREATE INDEX ix_spatial_index_cell ON spatial_index(s2_cell_id);

CREATE TABLE timespan_integration (
    timespan_integration_id TEXT PRIMARY KEY NOT NULL,
    temporal_expression_id TEXT NOT NULL UNIQUE REFERENCES temporal_expression(temporal_expression_id),
    method TEXT NOT NULL,
    kind TEXT,
    referential TEXT,
    temporal_source TEXT,
    lower_unix_utc_us INTEGER,
    upper_unix_utc_us INTEGER,
    offset_to_local_hours_lower TEXT,
    offset_to_local_hours_upper TEXT,
    lower_accuracy_us INTEGER,
    upper_accuracy_us INTEGER
);

CREATE TABLE analysis (
    analysis_id TEXT PRIMARY KEY NOT NULL,
    observation_id TEXT NOT NULL REFERENCES observation(observation_id),
    method TEXT NOT NULL,
    record_rank INTEGER,
    record_rank_type TEXT CHECK (record_rank_type IN ('main', 'additional') OR record_rank_type IS NULL),
    UNIQUE (observation_id, method)
);

CREATE TABLE edit_log (
    edit_id TEXT PRIMARY KEY NOT NULL,
    ordinal INTEGER NOT NULL UNIQUE,
    table_name TEXT,
    row_id TEXT,
    column TEXT,
    old_value TEXT,
    new_value TEXT,
    edit_kind TEXT CHECK (edit_kind IN ('update', 'new_row') OR edit_kind IS NULL),
    edited_by TEXT,
    edited_at TEXT
);

CREATE VIEW duplicate_sources AS
    SELECT fingerprint_sha256, COUNT(*) AS copies,
           GROUP_CONCAT(input_file_path, ' | ') AS files
    FROM source_file GROUP BY fingerprint_sha256 HAVING COUNT(*) > 1;

CREATE VIEW possible_same_evidence AS
    SELECT a.spatiotemporal_entity_hash, COUNT(*) AS copies,
           GROUP_CONCAT(s.input_file_path, ' | ') AS files
    FROM observation a
    JOIN raw_record r ON r.raw_record_id = a.raw_record_id
    JOIN source_file s ON s.source_file_id = r.source_file_id
    GROUP BY a.spatiotemporal_entity_hash HAVING COUNT(*) > 1;
