BEGIN;

CREATE SCHEMA IF NOT EXISTS orion_catalog;
CREATE SCHEMA IF NOT EXISTS orion_data;

CREATE TABLE IF NOT EXISTS orion_catalog.datasets (
    dataset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    source_uri TEXT,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IMPORTING', 'READY', 'SUPERSEDED', 'FAILED')),
    sheet_count INTEGER NOT NULL DEFAULT 0 CHECK (sheet_count >= 0),
    row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (project_id, document_id, version)
);

CREATE TABLE IF NOT EXISTS orion_catalog.sheets (
    sheet_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES orion_catalog.datasets(dataset_id),
    sheet_index INTEGER NOT NULL CHECK (sheet_index > 0),
    source_name TEXT NOT NULL,
    table_name TEXT NOT NULL UNIQUE,
    view_name TEXT NOT NULL,
    header_row INTEGER NOT NULL DEFAULT 1 CHECK (header_row > 0),
    row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    column_count INTEGER NOT NULL DEFAULT 0 CHECK (column_count >= 0),
    formula_count BIGINT NOT NULL DEFAULT 0 CHECK (formula_count >= 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (dataset_id, sheet_index),
    UNIQUE (dataset_id, source_name)
);

ALTER TABLE orion_catalog.sheets
    ADD COLUMN IF NOT EXISTS view_name TEXT;
UPDATE orion_catalog.sheets
SET view_name = 'current_' || substr(md5(sheet_id), 1, 16)
WHERE view_name IS NULL OR view_name = '';
ALTER TABLE orion_catalog.sheets
    ALTER COLUMN view_name SET NOT NULL;

CREATE TABLE IF NOT EXISTS orion_catalog.columns (
    sheet_id TEXT NOT NULL REFERENCES orion_catalog.sheets(sheet_id),
    column_index INTEGER NOT NULL CHECK (column_index > 0),
    source_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    inferred_type TEXT NOT NULL CHECK (
        inferred_type IN ('boolean', 'bigint', 'numeric', 'date', 'timestamp', 'text')
    ),
    nullable BOOLEAN NOT NULL,
    non_null_count BIGINT NOT NULL DEFAULT 0 CHECK (non_null_count >= 0),
    distinct_count BIGINT NOT NULL DEFAULT 0 CHECK (distinct_count >= 0),
    minimum_value TEXT,
    maximum_value TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (sheet_id, column_index),
    UNIQUE (sheet_id, column_name)
);

CREATE TABLE IF NOT EXISTS orion_catalog.current_datasets (
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL REFERENCES orion_catalog.datasets(dataset_id),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    version TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    promoted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, document_id)
);

CREATE TABLE IF NOT EXISTS orion_catalog.source_bindings (
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    engine TEXT NOT NULL CHECK (engine IN ('POSTGRESQL', 'MYSQL')),
    connection_ref TEXT NOT NULL,
    database_name TEXT NOT NULL,
    catalog_name TEXT,
    schemas TEXT[] NOT NULL,
    authorized_tables TEXT[] NOT NULL,
    authorized_columns JSONB NOT NULL DEFAULT '{}'::jsonb,
    access_mode TEXT NOT NULL CHECK (access_mode = 'READ_ONLY'),
    readonly_attested BOOLEAN NOT NULL,
    pii_scope TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'DISABLED', 'DEGRADED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, source_id)
);

CREATE TABLE IF NOT EXISTS orion_catalog.source_profiles (
    profile_id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    profiled_at TIMESTAMPTZ NOT NULL,
    schema_fingerprint TEXT NOT NULL CHECK (schema_fingerprint ~ '^sha256:[a-f0-9]{64}$'),
    receipt_sha256 TEXT NOT NULL CHECK (receipt_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    profile JSONB NOT NULL,
    FOREIGN KEY (project_id, source_id)
        REFERENCES orion_catalog.source_bindings(project_id, source_id)
);

CREATE TABLE IF NOT EXISTS orion_catalog.source_snapshots (
    dataset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    snapshot_version TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    snapshot_complete BOOLEAN NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    dataset_type TEXT NOT NULL CHECK (dataset_type IN ('PRODUCTION', 'TEST_ONLY')),
    production_evidence BOOLEAN NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (project_id, source_id)
        REFERENCES orion_catalog.source_bindings(project_id, source_id),
    UNIQUE (project_id, source_id, snapshot_version)
);

CREATE TABLE IF NOT EXISTS orion_catalog.snapshot_sets (
    snapshot_set_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    snapshot_version TEXT NOT NULL,
    promoted_at TIMESTAMPTZ NOT NULL,
    snapshot_complete BOOLEAN NOT NULL CHECK (snapshot_complete),
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    production_evidence BOOLEAN NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orion_catalog.snapshot_set_members (
    snapshot_set_id TEXT NOT NULL REFERENCES orion_catalog.snapshot_sets(snapshot_set_id),
    dataset_id TEXT NOT NULL REFERENCES orion_catalog.source_snapshots(dataset_id),
    source_id TEXT NOT NULL,
    PRIMARY KEY (snapshot_set_id, source_id),
    UNIQUE (snapshot_set_id, dataset_id)
);

CREATE TABLE IF NOT EXISTS orion_catalog.current_snapshot_sets (
    project_id TEXT PRIMARY KEY,
    snapshot_set_id TEXT NOT NULL REFERENCES orion_catalog.snapshot_sets(snapshot_set_id),
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    promoted_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS orion_catalog.identity_resolution_receipts (
    receipt_sha256 TEXT PRIMARY KEY CHECK (receipt_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    contract_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    snapshot_set_id TEXT NOT NULL REFERENCES orion_catalog.snapshot_sets(snapshot_set_id),
    resolved_at TIMESTAMPTZ NOT NULL,
    matched_identity_count BIGINT NOT NULL CHECK (matched_identity_count >= 0),
    unmatched_row_count BIGINT NOT NULL CHECK (unmatched_row_count >= 0),
    receipt JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS orion_data.canonical_identity_links (
    snapshot_set_id TEXT NOT NULL REFERENCES orion_catalog.snapshot_sets(snapshot_set_id),
    contract_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    canonical_key_sha256 TEXT NOT NULL CHECK (
        canonical_key_sha256 ~ '^sha256:[a-f0-9]{64}$'
    ),
    canonical_iri TEXT NOT NULL,
    source_rows JSONB NOT NULL,
    PRIMARY KEY (snapshot_set_id, contract_id, canonical_key_sha256)
);

CREATE TABLE IF NOT EXISTS orion_data.canonical_identity_members (
    snapshot_set_id TEXT NOT NULL REFERENCES orion_catalog.snapshot_sets(snapshot_set_id),
    contract_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    canonical_key_sha256 TEXT NOT NULL CHECK (
        canonical_key_sha256 ~ '^sha256:[a-f0-9]{64}$'
    ),
    canonical_iri TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL REFERENCES orion_catalog.source_snapshots(dataset_id),
    table_name TEXT NOT NULL,
    row_ordinal BIGINT NOT NULL CHECK (row_ordinal > 0),
    row_sha256 TEXT NOT NULL CHECK (row_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    PRIMARY KEY (
        snapshot_set_id, contract_id, canonical_key_sha256,
        source_id, dataset_id, row_ordinal
    )
);

CREATE TABLE IF NOT EXISTS orion_data.multi_source_snapshot_rows (
    dataset_id TEXT NOT NULL REFERENCES orion_catalog.source_snapshots(dataset_id),
    project_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_ordinal BIGINT NOT NULL CHECK (row_ordinal > 0),
    row_sha256 TEXT NOT NULL CHECK (row_sha256 ~ '^sha256:[a-f0-9]{64}$'),
    row_payload JSONB NOT NULL,
    PRIMARY KEY (dataset_id, row_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_datasets_project_status
    ON orion_catalog.datasets (project_id, status, imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_sheets_dataset
    ON orion_catalog.sheets (dataset_id, sheet_index);
CREATE INDEX IF NOT EXISTS idx_source_profiles_project_source
    ON orion_catalog.source_profiles (project_id, source_id, profiled_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_snapshots_project_source
    ON orion_catalog.source_snapshots (project_id, source_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_rows_lookup
    ON orion_data.multi_source_snapshot_rows (project_id, source_id, table_name);
CREATE INDEX IF NOT EXISTS idx_identity_links_project_snapshot
    ON orion_data.canonical_identity_links (project_id, snapshot_set_id, contract_id);
CREATE INDEX IF NOT EXISTS idx_identity_members_snapshot_source
    ON orion_data.canonical_identity_members (
        snapshot_set_id, contract_id, source_id, dataset_id, row_ordinal
    );

GRANT USAGE ON SCHEMA orion_catalog, orion_data TO orion_ingest_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA orion_catalog
    TO orion_ingest_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA orion_catalog TO orion_ingest_writer;
GRANT CREATE, USAGE ON SCHEMA orion_data TO orion_ingest_writer;

GRANT USAGE ON SCHEMA orion_catalog, orion_data TO orion_ontology_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA orion_catalog, orion_data TO orion_ontology_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA orion_catalog
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orion_ingest_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA orion_data
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO orion_ingest_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA orion_catalog
    GRANT SELECT ON TABLES TO orion_ontology_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA orion_data
    GRANT SELECT ON TABLES TO orion_ontology_reader;

COMMIT;
