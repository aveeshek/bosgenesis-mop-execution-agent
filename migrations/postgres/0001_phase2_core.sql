-- Phase 2 core persistence tables for bosgenesis-mop-execution-agent.
-- Payload columns store redacted JSON representations of the Pydantic models.

CREATE TABLE IF NOT EXISTS mop_execution_jobs (
  job_id TEXT PRIMARY KEY,
  bundle_id TEXT NOT NULL,
  target_namespace TEXT NOT NULL,
  state TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS mop_execution_jobs_state_idx
  ON mop_execution_jobs (state, updated_at DESC);

CREATE INDEX IF NOT EXISTS mop_execution_jobs_correlation_idx
  ON mop_execution_jobs (correlation_id);

CREATE TABLE IF NOT EXISTS mop_execution_phases (
  phase_pk BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  phase_id TEXT NOT NULL,
  status TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(job_id, phase_id)
);

CREATE TABLE IF NOT EXISTS mop_execution_steps (
  step_pk BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  phase_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  state TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(job_id, step_id)
);

CREATE TABLE IF NOT EXISTS mop_execution_observations (
  observation_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  phase_id TEXT NULL,
  step_id TEXT NULL,
  observation_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload_redacted JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS mop_execution_instructions (
  instruction_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  instruction_type TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload_redacted JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mop_execution_approvals (
  approval_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  approval_scope TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload_redacted JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS mop_execution_audit_events (
  audit_event_id TEXT PRIMARY KEY,
  job_id TEXT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE SET NULL,
  actor_type TEXT NOT NULL,
  action TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload_redacted JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS mop_execution_audit_job_created_idx
  ON mop_execution_audit_events (job_id, created_at);

CREATE TABLE IF NOT EXISTS mop_execution_idempotency_keys (
  idempotency_key TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_hash TEXT NULL,
  result_payload_redacted JSONB NULL,
  state TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS mop_execution_namespace_locks (
  lock_key TEXT PRIMARY KEY,
  target_namespace TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_expires_at TIMESTAMPTZ NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS mop_execution_namespace_locks_namespace_idx
  ON mop_execution_namespace_locks (target_namespace);

CREATE TABLE IF NOT EXISTS mop_execution_worker_heartbeats (
  worker_id TEXT PRIMARY KEY,
  job_id TEXT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE SET NULL,
  heartbeat_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS mop_execution_report_artifacts (
  report_id TEXT PRIMARY KEY,
  job_id TEXT NULL REFERENCES mop_execution_jobs(job_id) ON DELETE CASCADE,
  report_type TEXT NOT NULL,
  path TEXT NOT NULL,
  correlation_id TEXT NULL,
  trace_id TEXT NULL,
  payload_redacted JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);
