CREATE SCHEMA IF NOT EXISTS orion_workflow;

CREATE TABLE IF NOT EXISTS orion_workflow.schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orion_workflow.projects (
    project_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    intake_mode TEXT NOT NULL DEFAULT 'HYBRID',
    project_status TEXT NOT NULL,
    current_stage TEXT,
    workflow_version TEXT NOT NULL,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE TABLE IF NOT EXISTS orion_workflow.stage_states (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    fingerprint TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (project_id, stage)
);

CREATE TABLE IF NOT EXISTS orion_workflow.events (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    sequence BIGINT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    previous_event_hash TEXT,
    event_hash TEXT NOT NULL,
    current_stage TEXT,
    project_status TEXT,
    payload JSONB NOT NULL,
    PRIMARY KEY (project_id, sequence),
    UNIQUE (project_id, event_id),
    UNIQUE (project_id, event_hash)
);

CREATE TABLE IF NOT EXISTS orion_workflow.artifacts (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    path TEXT NOT NULL,
    stage TEXT,
    display_name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    content_type TEXT,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL,
    storage_backend TEXT NOT NULL,
    object_key TEXT,
    is_present BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (project_id, path)
);

ALTER TABLE orion_workflow.artifacts
    ADD COLUMN IF NOT EXISTS is_present BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_orion_artifacts_sha256
    ON orion_workflow.artifacts (sha256);
CREATE INDEX IF NOT EXISTS idx_orion_artifacts_stage
    ON orion_workflow.artifacts (project_id, stage);

CREATE TABLE IF NOT EXISTS orion_workflow.decisions (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    decision_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    item_id TEXT,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL,
    before_value JSONB,
    after_value JSONB,
    evidence JSONB,
    payload JSONB NOT NULL,
    PRIMARY KEY (project_id, decision_id)
);

CREATE TABLE IF NOT EXISTS orion_workflow.revisions (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    revision_id TEXT NOT NULL,
    target_stage TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    PRIMARY KEY (project_id, revision_id)
);

CREATE TABLE IF NOT EXISTS orion_workflow.releases (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    release_version TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_by TEXT,
    published_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    package_path TEXT,
    manifest_sha256 TEXT,
    payload JSONB NOT NULL,
    PRIMARY KEY (project_id, release_version)
);

CREATE TABLE IF NOT EXISTS orion_workflow.mcp_invocations (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    invocation_id TEXT NOT NULL,
    stage TEXT,
    service_name TEXT NOT NULL,
    tool_name TEXT,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms BIGINT,
    input_size_bytes BIGINT,
    output_reference TEXT,
    error_summary TEXT,
    payload JSONB NOT NULL,
    PRIMARY KEY (project_id, invocation_id)
);

CREATE OR REPLACE FUNCTION orion_workflow.reject_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '审计记录只允许追加，不允许更新或删除';
END;
$$;

DROP TRIGGER IF EXISTS events_are_append_only ON orion_workflow.events;
CREATE TRIGGER events_are_append_only
BEFORE UPDATE OR DELETE ON orion_workflow.events
FOR EACH ROW EXECUTE FUNCTION orion_workflow.reject_audit_mutation();

DROP TRIGGER IF EXISTS decisions_are_append_only ON orion_workflow.decisions;
CREATE TRIGGER decisions_are_append_only
BEFORE UPDATE OR DELETE ON orion_workflow.decisions
FOR EACH ROW EXECUTE FUNCTION orion_workflow.reject_audit_mutation();
