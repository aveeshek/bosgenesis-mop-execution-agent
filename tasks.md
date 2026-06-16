# BOS Genesis MoP Execution Agent - Implementation Tasks

This checklist is derived from `knowledge-base/PLAN.md`, `SPECS.md`, `OPENAPI.yaml`, and the MCP tool contract. Implementation must remain spec-driven:

- [ ] Keep every implementation task mapped to requirement IDs from `SPECS.md`.
- [ ] Keep the worker deterministic: no autonomous remediation, no direct LLM reasoning, no inferred fixes.
- [ ] Keep guardrails above all inputs: namespace scope, dry-run, human approval, redaction, idempotency, locks, audit.
- [ ] Treat `OPENAPI.yaml` as the concrete REST implementation target unless explicitly changed.
- [ ] Treat MCP tools as mirrors of the REST job-control surface using the standard result envelope.

## Phase 0 - Project Bootstrap

Goal: establish a clean, testable repository aligned to the specs. Requirement coverage: `[NFR-015]`.

- [x] [NFR-015] Create the target repository/package structure from `PLAN.md`.
- [x] [NFR-015] Copy or link canonical docs into root-friendly names: `SPECS.md`, `PLAN.md`, `HLD.md`, `LLD.md`, API and MCP contract docs.
- [x] [NFR-015] Add `pyproject.toml` with FastAPI, Pydantic, SQLAlchemy/SQLModel, async DB driver, Redis client, OpenTelemetry, pytest, ruff, mypy, and httpx.
- [x] [NFR-015] Add Dockerfile and `.env.example`.
- [x] [NFR-015] Add CI pipeline stages for lint, type check, unit tests, security tests, policy tests, and contract tests.
- [x] [NFR-015] Add sanitized sample MoP bundle under `tests/fixtures/sample_mop_bundle/`.
- [x] [NFR-015] Create `tests/REQUIREMENT_TRACEABILITY.md`.
- [x] [FR-038] Add minimal app skeleton with `/healthz`.
- [x] [FR-038] Verify `pytest`, `ruff`, `mypy`, and `/healthz` run locally.

## Phase 1 - Schemas, Models, and State Machine

Goal: define domain objects and deterministic transitions before execution. Requirement coverage: `[FR-001] [FR-002] [FR-025] [FR-028] [NFR-001] [NFR-002] [AC-021]`.

- [x] [FR-001][FR-002] Implement Pydantic API/domain models for jobs, phases, steps, observations, instructions, approvals, audit events, reports, resources, and policy blocks.
- [x] [FR-002][NFR-002] Implement job state enum from OpenAPI/MCP contract.
- [x] [FR-002][NFR-002] Implement step state enum from `SPECS.md`.
- [x] [NFR-002] Implement the state transition table and transition guard hooks.
- [x] [FR-028] Ensure every state change can emit an observation and audit event.
- [x] [NFR-013] Add correlation ID and trace ID fields across persisted and API models.
- [x] [NFR-008] Add deterministic error codes and problem detail mapping.
- [x] [NFR-002][AC-021] Add tests for allowed transitions, blocked transitions, restart-loadable state, and auditable state changes.

## Phase 2 - Persistence and Queuing

Goal: make jobs durable, idempotent, and restart-safe. Requirement coverage: `[FR-025] [FR-026] [FR-030] [FR-031] [NFR-001] [NFR-003] [NFR-006] [NFR-013]`.

- [x] [FR-025] Create PostgreSQL migrations for execution jobs, phases, steps, observations, instructions, approvals, audit events, idempotency keys, namespace locks, and report artifacts.
- [x] [FR-025] Implement repository layer for durable job, phase, step, observation, instruction, approval, and report records.
- [x] [FR-026][NFR-003] Implement append-only audit writer.
- [x] [FR-030] Implement idempotency key storage and replay behavior.
- [x] [FR-031][NFR-006] Implement Redis lease, heartbeat, and target namespace distributed lock.
- [x] [NFR-001] Implement restart/resume persistence behavior.
- [x] [FR-031][AC-024] Add namespace lock contention tests.
- [x] [FR-030] Add idempotent job creation and duplicate mutation request tests.
- [x] [FR-026][AC-022] Add audit append-only tests.

## Phase 3 - Artifact Bundle Reader and Validator

Goal: parse and validate MoP Creation Agent output bundles. Requirement coverage: `[FR-006] [FR-007] [FR-008] [FR-009] [FR-010] [FR-011] [FR-012] [AC-001] [AC-002] [AC-003]`.

- [ ] [FR-006][AC-001] Implement bundle source resolver for local path, uploaded archive, MoP creation run reference, artifact manifest, and object storage placeholder.
- [ ] [FR-006][FR-007] Implement `machine_execution_plan.yaml` parser and versioned schema.
- [ ] [FR-012] Implement dependency graph builder and cycle detection.
- [ ] [FR-008] Parse installation notes only as fallback/supporting context.
- [ ] [FR-008] Parse human MoP Markdown only as supporting context.
- [ ] [FR-011] Read `artifact.json`, `artifact-index.json`, and `response.json` when present.
- [ ] [FR-009] Load generated Kubernetes manifests and validate required fields, namespace, kind, name, scope, and references.
- [ ] [FR-010] Load Helm values files and validate redaction/safety posture.
- [ ] [AC-002][AC-003] Fail closed for missing machine plan, unsupported schema, invalid schema, graph cycles, missing files, invalid YAML, and unknown resource references.
- [ ] [AC-001] Add sample bundle validation tests proving machine plan is parsed first and Markdown cannot override it.

## Phase 4 - Redaction and Safety Policy Engine

Goal: enforce guardrails before execution exists. Requirement coverage: `[FR-015] [FR-016] [FR-017] [FR-018] [FR-019] [FR-020] [FR-030] [FR-032] [FR-033] [AC-005] [AC-006] [AC-007] [AC-008] [AC-009] [AC-010] [AC-023] [AC-028]`.

- [ ] [FR-018][AC-008] Implement namespace-scope guard.
- [ ] [FR-018][AC-009] Implement cluster-scoped resource blocker.
- [ ] [FR-019][AC-010] Implement Secret value detector for manifests, values, instructions, logs, and outputs.
- [ ] [FR-020] Implement production data copy detector and PVC data-copy blocker.
- [ ] [FR-015][AC-005] Implement dry-run gate independent of plan trust.
- [ ] [FR-016][FR-017][AC-006][AC-007] Implement approval gate, approval scope matcher, command fingerprinting, and expiration checks.
- [ ] [FR-030] Integrate idempotency guard with policy decisions.
- [ ] [FR-032] Add timeout and retry limit policy checks.
- [ ] [FR-033][AC-028] Implement redaction for strings, YAML, JSON, logs, events, values files, MCP responses, reports, and memory writes.
- [ ] [AC-023] Implement audit-before-mutation guard.
- [ ] [AC-010][AC-028] Add security tests with fake secrets, base64 payloads, passwords, tokens, connection strings, private keys, and sensitive env vars.

## Phase 5 - REST API and MCP Tool Surface

Goal: expose deterministic job control APIs and MCP tools. Requirement coverage: `[FR-001] [FR-002] [FR-003] [FR-004] [FR-005] [FR-037] [FR-038] [FR-039]`.

- [ ] [FR-038] Implement `/healthz`, `/readyz`, `/v1/capabilities`, and `/v1/config/effective`.
- [ ] [FR-001] Implement artifact bundle endpoints under `/v1/artifact-bundles`.
- [ ] [FR-001] Implement `POST /v1/execution-jobs`.
- [ ] [FR-002] Implement job, plan, observation, event, audit, memory-context, and report retrieval endpoints.
- [ ] [FR-003] Implement external instruction submission endpoint.
- [ ] [FR-004] Implement human approval submission endpoint.
- [ ] [FR-005] Implement start, pause, resume, cancel, and rollback request endpoints.
- [ ] [FR-039] Implement report list/download metadata endpoints.
- [ ] [FR-037] Implement MCP server tools mirroring REST: health, capabilities, register/validate bundle, create/get/list/start/pause/resume/cancel job, submit instruction/approval, get plan/decision/observations/audit/memory, evaluate policy, request rollback, generate release notes.
- [ ] [FR-037] Ensure every MCP tool returns the standard result envelope.
- [ ] [FR-038] Add auth dependency placeholder and redacted config behavior.
- [ ] [FR-001][FR-037] Add REST and MCP contract tests based on `OPENAPI.yaml` and `mcp_tool_contract.json`.

## Phase 6 - MCP Client Contracts

Goal: implement typed clients for governed MCP integrations. Requirement coverage: `[FR-021] [FR-022] [FR-023] [FR-024] [NFR-011]`.

- [ ] [NFR-011] Implement common MCP client base with timeout, structured results, safe transport retry, correlation IDs, redaction, structured errors, and audit hooks.
- [ ] [FR-021] Implement Kubernetes Inspector MCP client methods for namespace, dry-run apply, apply, get/list/describe, events, pod status/logs, delete, wait, and rollout checks.
- [ ] [FR-022] Implement Helm Manager MCP client methods for repo operations, template, dry-run install/upgrade, install/upgrade, status, history, rollback, and uninstall.
- [ ] [FR-023] Implement Data Ingestion MCP client for latest snapshot, historical facts, and recent events.
- [ ] [FR-024] Implement Release Note MCP client.
- [ ] [NFR-011] Add fake MCP servers for success, errors, timeouts, malformed responses, and redaction checks.
- [ ] [FR-021][FR-022][FR-023][FR-024] Add MCP contract tests proving failures create observations and never trigger worker reasoning.

## Phase 7 - Async Execution Runtime

Goal: execute plan phases and steps as a long-running external-control worker. Requirement coverage: `[FR-013] [FR-014] [FR-025] [FR-028] [FR-029] [FR-031] [FR-032] [AC-011] [AC-012] [AC-013] [AC-018] [AC-021] [AC-024]`.

- [ ] [FR-025] Implement queue and worker loop.
- [ ] [FR-013] Implement phase dependency scheduler and step selector.
- [ ] [FR-005] Implement pause, resume, cancel, and safe-stop handling.
- [ ] [FR-032] Implement wait/poll executor and timeout handling.
- [ ] [FR-028] Implement observation builder for state transitions, policy checks, MCP calls, dry-runs, mutations, validations, waits, errors, and memory writes.
- [ ] [FR-029] Implement decision-required context builder.
- [ ] [AC-021] Implement worker restart recovery.
- [ ] [FR-031][AC-024] Integrate target namespace lock acquisition/release.
- [ ] [FR-014] Ensure any failure, ambiguity, unexpected state, or policy issue pauses instead of reasoning.
- [ ] [AC-011][AC-012][AC-013][AC-018] Add runtime tests for long waits, timeouts, restarts, cancellations, and decision-required states.

## Phase 8 - Dry-Run Execution Path

Goal: safely execute dry-runs and preflight checks end to end. Requirement coverage: `[FR-015] [FR-021] [FR-022] [FR-028] [FR-040] [AC-004] [AC-012] [AC-015]`.

- [ ] [FR-015] Map plan command kinds to Kubernetes or Helm dry-run actions.
- [ ] [FR-021] Implement Kubernetes server-side dry-run apply executor.
- [ ] [FR-022] Implement Helm template and dry-run install/upgrade executor.
- [ ] [FR-040] Implement `dry_run_only` job mode that never mutates.
- [ ] [FR-028] Persist dry-run outputs as redacted observations.
- [ ] [AC-004] Add dry-run-only E2E test using sample bundle.
- [ ] [AC-012][AC-015] Add YAML syntax error and Helm render failure dry-run fixtures.

## Phase 9 - Approved Mutation Execution Path

Goal: execute namespace-scoped mutations only when all gates pass. Requirement coverage: `[FR-016] [FR-017] [FR-018] [FR-021] [FR-022] [AC-005] [AC-006] [AC-007] [AC-008] [AC-019] [AC-020]`.

- [ ] [FR-016][FR-017] Implement mutation gate pipeline: state, instruction, dry-run, approval, namespace, policy, lock, idempotency, and audit pre-event.
- [ ] [FR-021] Implement Kubernetes apply executor.
- [ ] [FR-022] Implement Helm install/upgrade executor.
- [ ] [FR-028] Persist mutation observations and resource mutation records.
- [ ] [NFR-001] Implement unknown mutation outcome handling.
- [ ] [AC-020] Add approved disposable namespace integration test.
- [ ] [AC-005][AC-006][AC-007][AC-008] Add tests proving mutation cannot occur without dry-run, approval, matching scope, and target namespace.
- [ ] [FR-030] Add duplicate instruction/request idempotency tests.

## Phase 10 - Failure Injection and External LLM Loop

Goal: prove the worker pauses and waits for explicit instruction. Requirement coverage: `[FR-003] [FR-014] [FR-029] [AC-011] [AC-012] [AC-013] [AC-014] [AC-015] [AC-016] [AC-017] [AC-018] [AC-019] [NFR-011]`.

- [ ] [FR-029] Implement decision-required endpoint and context packaging.
- [ ] [FR-003] Implement instruction validation and acceptance/rejection flow.
- [ ] [FR-026] Write audit events for instruction received, accepted, rejected, and policy-blocked.
- [ ] [AC-011] Add YAML syntax error fixture.
- [ ] [AC-012] Add dry-run failure fixture.
- [ ] [AC-013] Add resource already exists fixture.
- [ ] [AC-014] Add immutable field conflict fixture.
- [ ] [AC-015] Add Helm render failure fixture.
- [ ] [AC-016] Add PVC pending and pod unschedulable fixtures.
- [ ] [AC-017] Add node unavailable fixture.
- [ ] [AC-018] Add validation failure fixture.
- [ ] [NFR-011] Add ingress conflict, MCP outage, and timeout fixtures.
- [ ] [FR-014] For each fixture, assert worker pauses, emits correct reason code, captures redacted observations, labels memory as non-authoritative, and performs no repair without instruction.
- [ ] [AC-019] Add tests where valid external instructions resume safely.
- [ ] [AC-019] Add tests where unsafe instructions are blocked.

## Phase 11 - Memory and Audit Layers

Goal: implement memory as execution context only. Requirement coverage: `[FR-027] [FR-026] [FR-029] [AC-022] [AC-027] [AC-028] [NFR-003] [NFR-010]`.

- [ ] [FR-027] Implement in-run, durable job, resource state, episodic execution, semantic failure, policy, approval, audit, and observability memory layers.
- [ ] [FR-029] Add memory retrieval filters by namespace, chart, kind, error code, MCP source, tenant, and environment.
- [ ] [AC-027] Label all memory responses as `context_only_not_decision_authority`.
- [ ] [AC-027] Add tests proving memory cannot trigger execution or state transitions.
- [ ] [AC-028] Add redaction tests for all memory writes.
- [ ] [FR-026][AC-022] Add audit completeness tests for representative jobs.

## Phase 12 - Validation, Rollback, and Reports

Goal: produce operator-ready outputs after execution. Requirement coverage: `[FR-034] [FR-035] [FR-036] [FR-039] [AC-025] [AC-026]`.

- [ ] [FR-035] Implement validation executor for resources, rollout, pods, services, ingress, PVCs, Helm status, and custom plan validations.
- [ ] [FR-036] Implement rollback request flow requiring external instruction and human approval.
- [ ] [FR-036] Implement rollback executor through Helm and K8s MCPs.
- [ ] [FR-034] Implement execution report generator.
- [ ] [FR-035] Implement validation report generator.
- [ ] [FR-036] Implement rollback report generator.
- [ ] [FR-024][AC-026] Implement release-note MCP integration.
- [ ] [FR-039] Implement report artifact metadata and archive generation.
- [ ] [AC-025] Add report content, trace ID, warning, observation, and redaction tests.

## Phase 13 - Observability and Operations

Goal: make the service production-operable. Requirement coverage: `[NFR-009] [NFR-013] [FR-038]`.

- [ ] [NFR-009] Add structured JSON logging.
- [ ] [NFR-009] Add OpenTelemetry tracing and SigNoz exporter configuration.
- [ ] [NFR-009] Add metrics for job states, durations, MCP latency, policy blocks, decision-required counts, approval wait, redactions, audit failures, and lock contention.
- [ ] [FR-038] Finalize liveness/readiness probes.
- [ ] [NFR-013] Ensure logs, metrics, traces, and persisted records include job/correlation/trace IDs.
- [ ] [NFR-009] Add dashboard JSON placeholders or dashboard/runbook notes.
- [ ] [NFR-009] Add alerting recommendations.

## Phase 14 - Deployment and Runtime Configuration

Goal: package the service for deployment. Requirement coverage: `[NFR-012] [NFR-006]`.

- [x] [NFR-012] Finalize Dockerfile and runtime entrypoints.
- [x] [NFR-006] Create Helm chart or Kubernetes manifests.
- [x] [NFR-012] Define config map, secret references, environment variables, request limits, job limits, and policy configuration.
- [x] [NFR-006] Define service account, RBAC, network policy recommendations, resource requests/limits, and horizontal scaling guidance.
- [x] [NFR-006] Define worker concurrency and namespace lock behavior.
- [x] [FR-025] Define database migration job.
- [x] [NFR-012] Add deployment runbook, rollback runbook, and sample requests.
- [ ] [FR-040] Verify deployed service can run a sample dry-run job.

## Phase 15 - Release Candidate Hardening

Goal: validate production readiness. Requirement coverage: all MUST requirements and all acceptance criteria.

- [ ] Run full unit, policy, redaction, state-machine, contract, integration, E2E, security, and restart test suite.
- [ ] Run E2E sample bundle dry-run.
- [ ] Run E2E disposable namespace execution with approval.
- [ ] Run complete failure-injection matrix.
- [ ] Run restart/resume, lock contention, MCP outage, and long-running job tests.
- [ ] Run redaction/security scan across logs, fixtures, snapshots, traces, memory, reports, and persisted state.
- [ ] Run audit completeness verification.
- [ ] Verify documentation and runbooks.
- [ ] Produce release candidate report, final traceability matrix, known limitations, and release notes.
- [ ] Confirm all release gates G1-G12 pass or have approved deferrals.

## Mandatory Failure-Injection Checklist

Each scenario must assert deterministic block or `decision_required` with a complete, redacted context envelope.

- [ ] Malformed `machine_execution_plan.yaml`.
- [ ] Missing generated manifest file.
- [ ] Generated YAML syntax error.
- [ ] Manifest namespace mismatch.
- [ ] Cluster-scoped resource in bundle.
- [ ] Secret value detected in YAML.
- [ ] Production data copy command detected.
- [ ] Mutating step without dry-run.
- [ ] Mutating step without approval.
- [ ] Approval scope mismatch.
- [ ] Kubernetes server-side dry-run failure.
- [ ] Resource already exists.
- [ ] Immutable field conflict.
- [ ] Helm repo unavailable.
- [ ] Helm render failure.
- [ ] Helm release exists with conflicting metadata.
- [ ] PVC pending.
- [ ] Pod unschedulable.
- [ ] Node unavailable.
- [ ] Pod CrashLoopBackOff.
- [ ] Ingress host/path conflict.
- [ ] Validation timeout.
- [ ] MCP unavailable.
- [ ] Audit write failure.
- [ ] Worker restart during wait.
- [ ] Worker restart after dry-run before mutation.
- [ ] Duplicate resume request.
- [ ] Duplicate mutation instruction.
- [ ] Unsafe external LLM instruction.
- [ ] Expired approval.

## Release Gates

- [ ] G1: All unit tests pass.
- [ ] G2: All policy and redaction tests pass.
- [ ] G3: All state-machine transition tests pass.
- [ ] G4: MCP contract tests pass against fake servers.
- [ ] G5: Dry-run E2E sample bundle passes.
- [ ] G6: Approved mutation E2E disposable namespace passes.
- [ ] G7: Failure-injection matrix passes.
- [ ] G8: Restart/resume tests pass.
- [ ] G9: Audit completeness tests pass.
- [ ] G10: No Secret values in persisted state, logs, traces, memory, or reports.
- [ ] G11: Documentation and runbooks complete.
- [ ] G12: Observability verified.

## Documentation Deliverables

- [ ] `SPECS.md`.
- [ ] `PLAN.md`.
- [ ] `HLD.md`.
- [ ] `LLD.md`.
- [ ] `MACHINE_EXECUTION_PLAN_SCHEMA.md`.
- [ ] `POLICY.md`.
- [ ] `MEMORY.md`.
- [ ] `MCP_CONTRACTS.md`.
- [ ] `API.md`.
- [ ] `DEPLOYMENT.md`.
- [ ] `SAMPLE_REQUESTS.md`.
- [ ] `RUNBOOK.md`.
- [ ] `SECURITY_AND_REDACTION.md`.
- [ ] `RELEASE_CANDIDATE_REPORT.md`.

## Definition of Ready for Implementation Tickets

- [ ] Requirement IDs are listed.
- [ ] Expected behavior is explicit.
- [ ] Forbidden behavior is explicit.
- [ ] Input/output schema is included when applicable.
- [ ] Tests to add are listed.
- [ ] Policy impact is described.
- [ ] Audit impact is described.
- [ ] Redaction impact is described.
- [ ] Migration impact is described when applicable.
- [ ] Acceptance criteria are stated.

## Definition of Done for Implementation Tickets

- [ ] Code is implemented.
- [ ] Tests pass.
- [ ] Requirement traceability is updated.
- [ ] Policy and redaction impacts are tested.
- [ ] Audit events are covered.
- [ ] State transitions are valid.
- [ ] Docs are updated when behavior changes.
- [ ] No raw secrets appear in logs, fixtures, snapshots, memory, reports, or persisted state.
