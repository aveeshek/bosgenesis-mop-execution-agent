-- Phase 11: execution memory as non-authoritative context only.

CREATE TABLE IF NOT EXISTS mop_execution_memory_records (
    memory_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
    namespace TEXT,
    chart TEXT,
    kind TEXT,
    resource_name TEXT,
    error_code TEXT,
    mcp_source TEXT,
    tenant TEXT,
    environment TEXT,
    summary TEXT NOT NULL,
    payload_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    authority TEXT NOT NULL DEFAULT 'context_only_not_decision_authority',
    redaction_applied BOOLEAN NOT NULL DEFAULT TRUE,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mop_execution_memory_authority_context_only
        CHECK (authority = 'context_only_not_decision_authority'),
    CONSTRAINT mop_execution_memory_redacted_only
        CHECK (redaction_applied = TRUE)
);

CREATE INDEX IF NOT EXISTS idx_mop_execution_memory_job_created
    ON mop_execution_memory_records (job_id, created_at);

CREATE INDEX IF NOT EXISTS idx_mop_execution_memory_filters
    ON mop_execution_memory_records (
        namespace,
        chart,
        kind,
        error_code,
        mcp_source,
        tenant,
        environment
    );
