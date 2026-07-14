-- Operator-approved rollback for 0003_namespace_twin_foundation.sql.
-- Export/archive required twin audit records before running this destructive rollback.

DROP TABLE IF EXISTS namespace_twin_decisions;
DROP TABLE IF EXISTS namespace_twin_findings;
DROP TABLE IF EXISTS namespace_twin_edges;
DROP TABLE IF EXISTS namespace_twin_resources;
DROP TABLE IF EXISTS namespace_twin_events;
DROP TABLE IF EXISTS namespace_twin_runs;
