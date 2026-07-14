-- Namespace Digital Twin Phase 4 durable foundation.

CREATE TABLE IF NOT EXISTS namespace_twin_runs (
    twin_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    lifecycle_status VARCHAR(40) NOT NULL,
    decision VARCHAR(32) NOT NULL DEFAULT 'pending',
    decision_version INTEGER NOT NULL DEFAULT 1,
    decision_is_final BOOLEAN NOT NULL DEFAULT FALSE,
    source_type VARCHAR(40) NOT NULL,
    source_value_redacted TEXT NOT NULL,
    source_namespace TEXT,
    target_cluster TEXT NOT NULL DEFAULT 'configured-cluster',
    target_namespace TEXT NOT NULL,
    bundle_name TEXT NOT NULL,
    bundle_hash VARCHAR(64) NOT NULL,
    release_version TEXT,
    input_hash VARCHAR(64) NOT NULL,
    report_hash VARCHAR(64),
    policy_version TEXT NOT NULL,
    risk_rule_version TEXT NOT NULL,
    facts_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    actions_redacted JSONB NOT NULL DEFAULT '[]'::jsonb,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    superseded_by TEXT,
    CONSTRAINT uq_namespace_twin_scoped_idempotency
        UNIQUE (actor_id, target_namespace, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_namespace_twin_status_updated
    ON namespace_twin_runs (lifecycle_status, updated_at);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_target_created
    ON namespace_twin_runs (target_namespace, created_at);

CREATE TABLE IF NOT EXISTS namespace_twin_events (
    event_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES namespace_twin_runs(twin_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    message TEXT NOT NULL,
    payload_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_namespace_twin_event_sequence UNIQUE (twin_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_events_ordered
    ON namespace_twin_events (twin_id, sequence);

CREATE TABLE IF NOT EXISTS namespace_twin_resources (
    resource_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES namespace_twin_runs(twin_id) ON DELETE CASCADE,
    stable_identity TEXT NOT NULL,
    api_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    namespace TEXT,
    payload_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_namespace_twin_resource_identity UNIQUE (twin_id, stable_identity)
);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_resource_kind
    ON namespace_twin_resources (twin_id, kind);

CREATE TABLE IF NOT EXISTS namespace_twin_edges (
    edge_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES namespace_twin_runs(twin_id) ON DELETE CASCADE,
    source_identity TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    edge_type VARCHAR(80) NOT NULL,
    confidence VARCHAR(24) NOT NULL DEFAULT 'deterministic',
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT uq_namespace_twin_edge
        UNIQUE (twin_id, source_identity, target_identity, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_edge_source
    ON namespace_twin_edges (twin_id, source_identity);

CREATE TABLE IF NOT EXISTS namespace_twin_findings (
    finding_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES namespace_twin_runs(twin_id) ON DELETE CASCADE,
    code VARCHAR(100) NOT NULL,
    severity VARCHAR(24) NOT NULL,
    status VARCHAR(24) NOT NULL,
    message TEXT NOT NULL,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_finding_severity
    ON namespace_twin_findings (twin_id, severity);

CREATE TABLE IF NOT EXISTS namespace_twin_decisions (
    decision_id TEXT PRIMARY KEY,
    twin_id TEXT NOT NULL REFERENCES namespace_twin_runs(twin_id) ON DELETE CASCADE,
    decision_version INTEGER NOT NULL,
    decision VARCHAR(24) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    report_hash VARCHAR(64) NOT NULL,
    policy_version TEXT NOT NULL,
    risk_rule_version TEXT NOT NULL,
    facts_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_namespace_twin_decision_version UNIQUE (twin_id, decision_version)
);
CREATE INDEX IF NOT EXISTS idx_namespace_twin_decision_created
    ON namespace_twin_decisions (twin_id, created_at);

-- Retention: application policy expires active rows and archives terminal rows before deletion.
-- Cascades intentionally remove subordinate events/facts only when a run is explicitly deleted.
