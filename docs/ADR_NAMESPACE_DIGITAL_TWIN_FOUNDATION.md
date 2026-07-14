# ADR: Namespace Digital Twin Foundation

**Status:** Accepted for Phase 4  
**Date:** 2026-07-14

## Decision

The Namespace Digital Twin core is owned by the existing MoP Execution Agent. No new
baseline microservice and no new MCP server are introduced. ESDA is the authenticated
gateway and presentation layer; it does not calculate or reinterpret a twin decision.

The foundation reuses the execution agent's bundle reader, machine-plan parser,
dependency validation, namespace/cluster-scope enforcement, and authoritative dry-run
contract. Phase 4 stops at `awaiting_dry_run`; it never invents a Green, Amber, or Red
decision and never runs a second dry-run.

## Frozen Contracts

- Opaque `TEXT` identifiers are used for twin, event, resource, edge, finding, and decision IDs.
- Lifecycle states are `requested`, `generating`, `awaiting_dry_run`,
  `decision_calculating`, and terminal `green`, `amber`, `red`, `failed`, `cancelled`,
  `superseded`, or `expired`.
- Every run stores deterministic `input_hash` and bundle hash. Terminal versions also
  store `report_hash`, policy version, and risk-rule version.
- Terminal decision versions are append-only and immutable.
- Events and stored facts are redacted before persistence.
- Idempotency is scoped by actor, target namespace, and idempotency key.
- Omission is never interpreted as deletion. Only explicit delete steps/commands are parsed.

## Mixed UI

During Phase 4, list/detail lifecycle and audit facts come from the real core. Evidence
tabs remain deterministic mock modules and are labeled `Real Core + Mock Modules` and
`Mock / Non-authoritative`. Mock tab data cannot affect real action eligibility.

## Persistence and Rollback

Production uses PostgreSQL through `NAMESPACE_TWIN_DATABASE_URL`; local and contract tests
may use SQLite. Apply `migrations/postgres/0003_namespace_twin_foundation.sql`. Before any
rollback, archive audit records, stop twin workers, then run the companion rollback SQL.
