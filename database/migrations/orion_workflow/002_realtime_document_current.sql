CREATE TABLE IF NOT EXISTS orion_workflow.document_version_receipts (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    document_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    version TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    graph_uri TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL,
    promoted_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (project_id, document_id, file_sha256),
    UNIQUE (graph_uri)
);

CREATE TABLE IF NOT EXISTS orion_workflow.document_current_versions (
    project_id TEXT NOT NULL REFERENCES orion_workflow.projects(project_id),
    document_id TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    version TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    graph_uri TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL,
    promoted_at TIMESTAMPTZ NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (project_id, document_id),
    UNIQUE (graph_uri),
    FOREIGN KEY (project_id, document_id, file_sha256)
        REFERENCES orion_workflow.document_version_receipts (
            project_id, document_id, file_sha256
        )
);

CREATE INDEX IF NOT EXISTS idx_orion_document_current_graph
    ON orion_workflow.document_current_versions (graph_uri);

DROP TRIGGER IF EXISTS document_version_receipts_are_append_only
    ON orion_workflow.document_version_receipts;
CREATE TRIGGER document_version_receipts_are_append_only
BEFORE UPDATE OR DELETE ON orion_workflow.document_version_receipts
FOR EACH ROW EXECUTE FUNCTION orion_workflow.reject_audit_mutation();
