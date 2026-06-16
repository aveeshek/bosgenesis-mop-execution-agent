# BOS Genesis MoP Execution Agent - PLAN

**Document status:** Draft v0.1  
**Document name:** `PLAN.md`  
**Agent name:** `bosgenesis-mop-execution-agent`  
**Development approach:** Spec-driven development  
**Primary spec:** `SPECS.md`  
**Primary design references:** HLD and LLD for the MoP Execution Agent  
**Primary implementation rule:** No autonomous reasoning authority in the worker  

---

## 1. Development Philosophy

This project must be built using spec-driven development.

```text
SPECS.md defines the expected behavior.
Tests encode SPECS.md.
Implementation satisfies tests.
Design documents explain why the implementation exists.
```

The implementation must never drift into an autonomous reasoning agent. The worker executes explicit machine plans and explicit external LLM instructions, while enforcing schema, scope, approval, idempotency, redaction, concurrency locks, timeout limits, and audit logging.

---

## 2. Source-of-Truth Order

When documents disagree, use this order:

1. `SPECS.md`
2. Security and policy configuration
3. MoP Execution Agent LLD
4. MoP Execution Agent HLD
5. Machine execution plan schema
6. MoP Creation Agent output contracts
7. Implementation comments

Guardrails override every document, input, approval, and instruction.

---

## 3. Requirement Traceability Rules

Every implementation item must map to one or more requirement IDs from `SPECS.md`.

Required traceability format:

```text
[FR-015][FR-016][AC-005][AC-006] Implement mutation gate requiring dry-run and approval.
```

Every pull request must include:

- requirement IDs implemented;
- tests added or updated;
- policy impact;
- state-machine impact;
- redaction impact;
- audit impact;
- backward compatibility impact.

---

## 4. Target Repository Structure

```text
bosgenesis-mop-execution-agent/
  README.md
  SPECS.md
  PLAN.md
  HLD.md
  LLD.md
  pyproject.toml
  Dockerfile
  .env.example
  config/
    settings.yaml
    policy.namespace-only-v1.yaml
    redaction.yaml
    state_machine.yaml
    machine_execution_plan.schema.yaml
  src/
    bosgenesis_mop_execution_agent/
      __init__.py
      api/
        app.py
        routes.py
        dependencies.py
        error_handlers.py
        schemas.py
      mcp_server/
        server.py
        tools.py
        schemas.py
      core/
        orchestrator.py
        job_service.py
        phase_service.py
        step_service.py
        execution_loop.py
        decision_context.py
      plans/
        machine_plan_parser.py
        machine_plan_schema.py
        dependency_graph.py
        plan_models.py
      artifacts/
        bundle_reader.py
        bundle_validator.py
        artifact_index.py
        manifest_loader.py
        values_loader.py
        checksum.py
      policy/
        engine.py
        namespace_scope.py
        approval_gate.py
        dry_run_gate.py
        secret_guard.py
        production_data_guard.py
        command_fingerprint.py
        idempotency.py
      state/
        machine.py
        transitions.py
        locks.py
        leases.py
      execution/
        action_models.py
        kubernetes_executor.py
        helm_executor.py
        validation_executor.py
        rollback_executor.py
        wait_executor.py
      mcp_clients/
        base.py
        k8s_inspector.py
        helm_manager.py
        data_ingestion.py
        release_notes.py
      memory/
        service.py
        models.py
        in_run.py
        durable_job.py
        resource_state.py
        episodic.py
        semantic_failure.py
        approval.py
        audit.py
      persistence/
        postgres.py
        repositories.py
        migrations/
        clickhouse.py
        redis.py
        object_store.py
      observability/
        logging.py
        metrics.py
        tracing.py
        audit_events.py
      reports/
        execution_report.py
        validation_report.py
        rollback_report.py
        release_notes.py
        archive.py
      security/
        redaction.py
        sensitive_patterns.py
        auth.py
      common/
        ids.py
        time.py
        errors.py
        result.py
  tests/
    unit/
    contract/
    integration/
    e2e/
    security/
    fixtures/
      sample_mop_bundle/
```

---

## 5. Milestone Overview

| Milestone | Name | Outcome |
|---|---|---|
| M0 | Repository and spec baseline | Project skeleton, docs, schemas, CI bootstrap. |
| M1 | Core models and state machine | Durable job, phase, step, observation, approval, instruction, and state transitions. |
| M2 | Artifact bundle ingestion | Machine plan parsing, bundle validation, manifest/value loading. |
| M3 | Policy and redaction engine | Namespace, dry-run, approval, Secret, data-copy, idempotency, and command fingerprint gates. |
| M4 | MCP client contracts | Typed clients and contract tests for K8s, Helm, data ingestion, and release-note MCPs. |
| M5 | Async execution runtime | Long-running worker loop, queue, locks, pause/resume/cancel, decision-required context. |
| M6 | Dry-run execution | End-to-end dry-run-only jobs using sample bundle. |
| M7 | Approved mutation execution | Approved namespace-scoped apply/install path in disposable namespace. |
| M8 | Failure handling and external LLM loop | Injected failures pause with structured context and resume only on valid instruction. |
| M9 | Memory and audit | Durable memory, semantic failure context, append-only audit, redacted observations. |
| M10 | Reports and release notes | Execution report, validation report, rollback report, release-note MCP integration. |
| M11 | Observability and operations | Logs, metrics, traces, health, readiness, deployment manifests. |
| M12 | Hardening and release candidate | Security, restart, concurrency, performance, docs, runbooks, final test matrix. |

---

## 6. Detailed Implementation Plan

## Phase 0 - Project Bootstrap

**Goal:** Establish a clean, testable repository aligned to `SPECS.md`.

Requirement coverage: `[NFR-015]`

Tasks:

- Create repository structure.
- Add `SPECS.md`, `PLAN.md`, `HLD.md`, and `LLD.md`.
- Add `pyproject.toml` with FastAPI, Pydantic, SQLAlchemy or SQLModel, async DB driver, Redis client, OpenTelemetry, pytest, ruff, mypy, and httpx.
- Add Dockerfile.
- Add `.env.example`.
- Add CI pipeline for lint, type check, unit tests, and security checks.
- Add sample MoP output bundle under `tests/fixtures/sample_mop_bundle/`.
- Create requirement traceability file `tests/REQUIREMENT_TRACEABILITY.md`.

Deliverables:

- bootable service skeleton;
- CI baseline;
- docs checked into repository.

Exit criteria:

- `pytest` runs;
- `ruff` and `mypy` run;
- app starts with `/healthz` returning healthy.

---

## Phase 1 - Schemas, Models, and State Machine

**Goal:** Define all domain objects and deterministic state transitions before implementing execution.

Requirement coverage: `[FR-001] [FR-002] [FR-025] [FR-028] [NFR-001] [NFR-002] [AC-021]`

Tasks:

- Implement Pydantic models:
  - `ExecutionJob`;
  - `ExecutionPhase`;
  - `ExecutionStep`;
  - `Observation`;
  - `Instruction`;
  - `Approval`;
  - `AuditEvent`;
  - `ExecutionReportRef`.
- Implement job states from `SPECS.md`.
- Implement step states from `SPECS.md`.
- Implement transition table.
- Add transition guard hooks.
- Add state-machine unit tests for every allowed and blocked transition.
- Add deterministic error codes.
- Add correlation ID and trace ID propagation fields.

Deliverables:

- `state/machine.py`;
- `state/transitions.py`;
- model schemas;
- transition tests.

Exit criteria:

- invalid transitions are rejected;
- restart state can be loaded and resumed in tests;
- all state changes are represented as auditable events.

---

## Phase 2 - Persistence and Queuing

**Goal:** Make jobs durable and restart-safe.

Requirement coverage: `[FR-025] [FR-026] [FR-030] [FR-031] [NFR-001] [NFR-003] [NFR-006] [NFR-013]`

Tasks:

- Design PostgreSQL migrations:
  - `execution_jobs`;
  - `execution_phases`;
  - `execution_steps`;
  - `observations`;
  - `instructions`;
  - `approvals`;
  - `audit_events`;
  - `idempotency_keys`;
  - `namespace_locks`;
  - `report_artifacts`.
- Implement repository layer.
- Implement Redis lease/heartbeat layer.
- Implement target namespace distributed lock.
- Implement idempotency key storage.
- Implement append-only audit writer.
- Add migration tests.
- Add restart/resume persistence tests.

Deliverables:

- database schema;
- repository layer;
- lock service;
- idempotency service;
- audit writer.

Exit criteria:

- job created in database;
- idempotent job creation works;
- duplicate mutations are safely deduplicated;
- namespace lock prevents concurrent mutating jobs.

---

## Phase 3 - Artifact Bundle Reader and Validator

**Goal:** Parse and validate MoP Creation Agent output bundles.

Requirement coverage: `[FR-006] [FR-007] [FR-008] [FR-009] [FR-010] [FR-011] [FR-012] [AC-001] [AC-002] [AC-003]`

Tasks:

- Implement bundle reference resolver:
  - local path;
  - uploaded zip;
  - MoP creation run reference;
  - artifact manifest reference;
  - object storage reference placeholder.
- Implement `machine_execution_plan.yaml` parser.
- Define versioned machine plan schema.
- Implement machine plan validation.
- Parse dependency graph.
- Detect graph cycles.
- Parse installation notes fallback only when standalone YAML is missing.
- Read human MoP Markdown as supporting context.
- Read `artifact.json`, `artifact-index.json`, and `response.json`.
- Load generated YAML manifests.
- Load Helm values files.
- Detect required files.
- Add tests for missing files, invalid schema, graph cycle, invalid YAML, unsupported schema, and unknown resource references.

Deliverables:

- `artifacts/bundle_reader.py`;
- `artifacts/bundle_validator.py`;
- `plans/machine_plan_parser.py`;
- `plans/dependency_graph.py`;
- machine plan schema.

Exit criteria:

- sample bundle validates;
- invalid bundle fails closed;
- machine plan is always parsed first;
- supporting Markdown never overrides the machine plan.

---

## Phase 4 - Redaction and Safety Policy Engine

**Goal:** Enforce the guardrails before any MCP execution exists.

Requirement coverage: `[FR-015] [FR-016] [FR-017] [FR-018] [FR-019] [FR-020] [FR-030] [FR-032] [FR-033] [AC-005] [AC-006] [AC-007] [AC-008] [AC-009] [AC-010] [AC-023] [AC-028]`

Tasks:

- Implement namespace-scope guard.
- Implement cluster-scope blocker.
- Implement Secret value detector.
- Implement production data copy detector.
- Implement PVC data copy blocker.
- Implement dry-run gate.
- Implement approval gate.
- Implement command fingerprint generation.
- Implement approval scope matcher.
- Implement expiration checks for instructions and approvals.
- Implement redaction engine for strings, YAML, JSON, logs, events, values files, and MCP responses.
- Implement audit-before-mutation guard.
- Add security tests using fake secrets, base64 payloads, passwords, tokens, and connection strings.

Deliverables:

- `policy/engine.py`;
- `policy/namespace_scope.py`;
- `policy/approval_gate.py`;
- `policy/dry_run_gate.py`;
- `policy/secret_guard.py`;
- `security/redaction.py`.

Exit criteria:

- policy unit tests pass;
- mutation without dry-run and approval is impossible;
- Secret values never appear in test snapshots;
- cluster-scoped resources are blocked.

---

## Phase 5 - REST API and MCP Tool Surface

**Goal:** Expose deterministic job control APIs and MCP tools.

Requirement coverage: `[FR-001] [FR-002] [FR-003] [FR-004] [FR-005] [FR-037] [FR-038] [FR-039]`

Tasks:

- Implement REST endpoints:
  - create job;
  - get job;
  - list jobs;
  - submit instruction;
  - submit approval;
  - resume;
  - pause;
  - cancel;
  - request rollback;
  - get observations;
  - get audit events;
  - download reports;
  - health/readiness/config.
- Implement MCP server tools mirroring REST endpoints.
- Add auth dependency placeholder.
- Add redacted config endpoint.
- Add OpenAPI examples.
- Add API validation tests.
- Add MCP tool contract tests.

Deliverables:

- FastAPI app;
- MCP server;
- request/response schemas;
- API tests.

Exit criteria:

- jobs can be created via REST and MCP;
- invalid instructions are rejected;
- invalid approvals are rejected;
- health/readiness/config work.

---

## Phase 6 - MCP Client Contracts

**Goal:** Implement typed, testable clients for the governed MCP ecosystem.

Requirement coverage: `[FR-021] [FR-022] [FR-023] [FR-024] [NFR-011]`

Tasks:

- Implement common MCP client base:
  - timeout;
  - retry transport only for safe idempotent reads;
  - correlation IDs;
  - redaction;
  - structured errors;
  - audit hooks.
- Implement Kubernetes Inspector MCP client:
  - namespace get/create;
  - server-side dry-run apply;
  - apply manifest;
  - get/list resources;
  - events;
  - pod status/logs;
  - delete;
  - wait conditions.
- Implement Helm Manager MCP client:
  - repo operations;
  - template/render;
  - dry-run install/upgrade;
  - install/upgrade;
  - status/history;
  - rollback/uninstall.
- Implement Data Ingestion MCP client:
  - latest snapshot;
  - historical facts;
  - prior events.
- Implement Release Note MCP client.
- Add fake MCP servers for tests.
- Add contract tests for success, errors, timeouts, malformed responses, and redaction.

Deliverables:

- `mcp_clients/` package;
- fake MCP test servers;
- MCP contract test suite.

Exit criteria:

- all MCP clients return structured results;
- MCP failures produce observations and do not trigger worker reasoning;
- responses are redacted before persistence.

---

## Phase 7 - Async Execution Runtime

**Goal:** Execute plan phases and steps as a long-running, externally controlled worker.

Requirement coverage: `[FR-013] [FR-014] [FR-025] [FR-028] [FR-029] [FR-031] [FR-032] [AC-011] [AC-012] [AC-013] [AC-018] [AC-021] [AC-024]`

Tasks:

- Implement job queue and worker loop.
- Implement phase dependency scheduling.
- Implement step selection.
- Implement pause/resume/cancel handling.
- Implement wait/poll executor.
- Implement timeout handling.
- Implement observation builder.
- Implement decision-required context builder.
- Implement worker restart recovery.
- Implement target namespace lock acquisition/release.
- Implement action attempt numbering.
- Add tests for long-running wait, timeout, restart, cancellation, and decision-required states.

Deliverables:

- `core/execution_loop.py`;
- `core/decision_context.py`;
- queue/worker service;
- runtime tests.

Exit criteria:

- dry-run steps can be scheduled;
- failures pause with full context;
- jobs resume after restart;
- locks prevent competing mutation jobs.

---

## Phase 8 - Dry-Run Execution Path

**Goal:** Safely execute dry-runs and preflight checks end-to-end.

Requirement coverage: `[FR-015] [FR-021] [FR-022] [FR-028] [FR-040] [AC-004] [AC-012] [AC-015]`

Tasks:

- Map plan command kinds to Kubernetes or Helm MCP dry-run actions.
- Implement Kubernetes server-side dry-run apply executor.
- Implement Helm template/dry-run executor.
- Implement namespace verification preflight.
- Implement artifact-bundle preflight.
- Implement validation preflight.
- Persist dry-run outputs as redacted observations.
- Add dry-run-only E2E test using sample bundle.
- Inject YAML syntax error and Helm render error fixtures.

Deliverables:

- dry-run execution engine;
- dry-run-only job mode;
- E2E dry-run test.

Exit criteria:

- sample bundle dry-run-only mode completes or pauses with expected warnings;
- dry-run failure produces `decision_required`;
- no mutation occurs in dry-run-only mode.

---

## Phase 9 - Approved Mutation Execution Path

**Goal:** Execute namespace-scoped mutations only when all gates pass.

Requirement coverage: `[FR-016] [FR-017] [FR-018] [FR-021] [FR-022] [AC-005] [AC-006] [AC-007] [AC-008] [AC-019] [AC-020]`

Tasks:

- Implement mutation gate pipeline:
  - state check;
  - instruction check if required;
  - dry-run check;
  - approval check;
  - namespace scope check;
  - policy check;
  - lock check;
  - audit pre-event check.
- Implement Kubernetes apply executor.
- Implement Helm install/upgrade executor.
- Implement mutation observation persistence.
- Implement uncertain outcome handling.
- Add disposable namespace integration test.
- Add duplicate request idempotency tests.
- Add approval scope mismatch tests.

Deliverables:

- mutation executor;
- approval-gated apply/install tests;
- idempotency tests.

Exit criteria:

- approved sample resources can be applied into a disposable namespace;
- no mutation happens without dry-run and approval;
- duplicate calls do not duplicate effects.

---

## Phase 10 - Failure Injection and External LLM Loop

**Goal:** Prove the worker does not reason and instead asks the external LLM for instructions.

Requirement coverage: `[FR-003] [FR-014] [FR-029] [AC-011] [AC-012] [AC-013] [AC-014] [AC-015] [AC-016] [AC-017] [AC-018] [AC-019] [NFR-011]`

Tasks:

- Implement decision-required context endpoint.
- Implement instruction validation.
- Implement instruction acceptance/rejection audit events.
- Add failure fixtures:
  - YAML syntax error;
  - resource already exists;
  - immutable field conflict;
  - Helm render failure;
  - PVC pending;
  - pod unschedulable;
  - node unavailable;
  - ingress conflict;
  - validation failure;
  - MCP outage;
  - timeout.
- For each fixture, assert:
  - worker pauses;
  - reason code is correct;
  - observations are captured;
  - memory context is labeled as non-authoritative;
  - no repair occurs without instruction.
- Add tests where an external LLM instruction resumes safely.
- Add tests where unsafe instruction is blocked.

Deliverables:

- failure-injection suite;
- instruction loop implementation;
- decision context examples.

Exit criteria:

- all injected failures result in `decision_required` or policy block;
- unsafe instructions cannot bypass guardrails;
- valid instructions can resume work.

---

## Phase 11 - Memory and Audit Layers

**Goal:** Implement all memory layers as execution memory only.

Requirement coverage: `[FR-027] [FR-026] [FR-029] [AC-022] [AC-027] [AC-028] [NFR-003] [NFR-010]`

Tasks:

- Implement in-run memory.
- Implement durable job memory.
- Implement resource state memory.
- Implement episodic execution memory.
- Implement semantic failure memory adapter.
- Implement policy memory loader.
- Implement approval memory.
- Implement audit memory.
- Implement observability memory metadata.
- Add memory retrieval filters by namespace, chart, kind, error code, and component.
- Label all memory as context-only.
- Add tests proving memory cannot trigger execution.
- Add redaction tests for memory writes.
- Add audit completeness tests.

Deliverables:

- memory service;
- audit event store;
- memory retrieval API for decision context;
- tests.

Exit criteria:

- memory context appears in decision envelopes;
- memory cannot cause state transition by itself;
- audit trail is complete for representative jobs.

---

## Phase 12 - Validation, Rollback, and Reports

**Goal:** Produce operator-ready outputs after execution.

Requirement coverage: `[FR-034] [FR-035] [FR-036] [FR-039] [AC-025] [AC-026]`

Tasks:

- Implement validation executor:
  - resource exists;
  - rollout available;
  - pod ready;
  - service exists;
  - ingress exists;
  - PVC bound;
  - Helm status deployed;
  - custom plan validations.
- Implement rollback request flow.
- Implement rollback executor through Helm and K8s MCPs.
- Require external instruction and human approval for rollback.
- Implement execution report generator.
- Implement validation report generator.
- Implement rollback report generator.
- Implement release-note MCP integration.
- Implement archive generation.
- Add tests for report contents and redaction.

Deliverables:

- `reports/` package;
- rollback service;
- report download APIs;
- release-note integration.

Exit criteria:

- final reports are produced for dry-run and execution jobs;
- rollback report is produced when rollback runs;
- reports contain trace IDs, job IDs, warnings, and redacted observations.

---

## Phase 13 - Observability and Operations

**Goal:** Make the service production-operable.

Requirement coverage: `[NFR-009] [NFR-013] [FR-038]`

Tasks:

- Add structured JSON logging.
- Add OpenTelemetry tracing.
- Add SigNoz exporter configuration.
- Add metrics:
  - job state counts;
  - step durations;
  - MCP latency;
  - policy blocks;
  - decision-required counts;
  - approval wait time;
  - redaction counts;
  - audit failures;
  - lock contention.
- Add health and readiness probes.
- Add liveness-safe worker heartbeat.
- Add operational dashboards or dashboard JSON placeholders.
- Add alerting recommendations.

Deliverables:

- observability module;
- metrics endpoint;
- deployment probes;
- dashboard/runbook notes.

Exit criteria:

- local traces show job, phase, step, MCP spans;
- metrics expose current job states;
- health/readiness reflect dependencies.

---

## Phase 14 - Deployment and Runtime Configuration

**Goal:** Package the service for deployment.

Requirement coverage: `[NFR-012] [NFR-006]`

Tasks:

- Finalize Dockerfile.
- Create Helm chart or Kubernetes manifests for the agent.
- Define config map and secret references.
- Define service account needs.
- Define network policy recommendations.
- Define resource requests/limits.
- Define horizontal scaling guidance.
- Define worker concurrency settings.
- Define database migration job.
- Add deployment runbook.
- Add rollback runbook.
- Add sample requests.

Deliverables:

- container image;
- deployment manifests/chart;
- config examples;
- deployment guide;
- sample requests.

Exit criteria:

- service deploys into test namespace;
- migrations run;
- health/readiness pass;
- sample dry-run job runs through deployed service.

---

## Phase 15 - Release Candidate Hardening

**Goal:** Validate production readiness.

Requirement coverage: all MUST requirements and all acceptance criteria.

Tasks:

- Run full test suite.
- Run E2E sample bundle dry-run.
- Run E2E disposable namespace execution with approval.
- Run failure-injection matrix.
- Run restart/resume test.
- Run lock contention test.
- Run redaction/security scan.
- Run audit completeness verification.
- Run performance test for long-running job.
- Run MCP outage test.
- Verify docs and runbooks.
- Produce release candidate report.

Deliverables:

- release candidate validation report;
- final traceability matrix;
- known limitations;
- release notes.

Exit criteria:

- all MUST requirements pass;
- all acceptance criteria pass or have approved deferral;
- no high or critical security findings;
- no Secret values in logs/traces/memory/reports;
- rollback runbook validated.

---

## 7. Testing Matrix

| Test group | Target | Required by |
|---|---|---|
| Parser tests | Machine plan, Markdown fallback, artifact metadata, generated YAML, values. | FR-006 to FR-012 |
| State-machine tests | Job and step transitions. | NFR-002, AC-021 |
| Policy tests | Namespace, dry-run, approval, Secret, data copy, cluster scope. | FR-015 to FR-020 |
| Redaction tests | Logs, YAML, JSON, values, observations, memory. | FR-033, AC-028 |
| MCP contract tests | K8s, Helm, data ingestion, release note. | FR-021 to FR-024 |
| API tests | REST and MCP job control. | FR-001 to FR-005, FR-037 |
| Dry-run E2E | Sample bundle dry-run-only. | FR-040, AC-004 |
| Mutation E2E | Disposable namespace apply/install with approval. | FR-016, AC-020 |
| Failure injection | YAML, dry-run, resource exists, immutable, Helm, PVC, pod, node, ingress, validation, MCP outage. | AC-011 to AC-018 |
| Restart tests | Durable resume after worker restart. | NFR-001, AC-021 |
| Audit tests | Append-only complete event trail. | FR-026, AC-022 |
| Lock tests | Namespace concurrency lock. | FR-031, AC-024 |
| Report tests | Execution, validation, rollback, release notes. | FR-034 to FR-036 |

---

## 8. Failure-Injection Scenarios

The following scenarios are mandatory before release candidate:

1. malformed `machine_execution_plan.yaml`;
2. missing generated manifest file;
3. generated YAML syntax error;
4. manifest namespace mismatch;
5. cluster-scoped resource in bundle;
6. Secret value detected in YAML;
7. production data copy command detected;
8. mutating step without dry-run;
9. mutating step without approval;
10. approval scope mismatch;
11. Kubernetes server-side dry-run failure;
12. resource already exists;
13. immutable field conflict;
14. Helm repo unavailable;
15. Helm render failure;
16. Helm release exists with conflicting metadata;
17. PVC pending;
18. pod unschedulable;
19. node unavailable;
20. pod CrashLoopBackOff;
21. ingress host/path conflict;
22. validation timeout;
23. MCP unavailable;
24. audit write failure;
25. worker restart during wait;
26. worker restart after dry-run before mutation;
27. duplicate resume request;
28. duplicate mutation instruction;
29. unsafe external LLM instruction;
30. expired approval.

Each scenario must assert that the worker either blocks deterministically or enters `decision_required` with a complete context envelope.

---

## 9. Release Gates

| Gate | Description | Required |
|---|---|---:|
| G1 | All unit tests pass. | Yes |
| G2 | All policy and redaction tests pass. | Yes |
| G3 | All state-machine transition tests pass. | Yes |
| G4 | MCP contract tests pass against fake servers. | Yes |
| G5 | Dry-run E2E sample bundle passes. | Yes |
| G6 | Approved mutation E2E disposable namespace passes. | Yes |
| G7 | Failure-injection matrix passes. | Yes |
| G8 | Restart/resume tests pass. | Yes |
| G9 | Audit completeness tests pass. | Yes |
| G10 | No Secret values in persisted state, logs, traces, memory, or reports. | Yes |
| G11 | Documentation and runbooks complete. | Yes |
| G12 | Observability verified. | Yes |

---

## 10. Initial Implementation Backlog

### Epic A - Core service foundation

- [ ] Create package skeleton.
- [ ] Add FastAPI app.
- [ ] Add MCP server skeleton.
- [ ] Add config loader.
- [ ] Add typed settings.
- [ ] Add health/readiness endpoints.

### Epic B - Domain and state

- [ ] Add Pydantic models.
- [ ] Add state enums.
- [ ] Add transition table.
- [ ] Add transition tests.
- [ ] Add error taxonomy.

### Epic C - Storage

- [ ] Add PostgreSQL migrations.
- [ ] Add repository layer.
- [ ] Add Redis lease support.
- [ ] Add idempotency support.
- [ ] Add audit event store.

### Epic D - Artifact parsing

- [ ] Add bundle reader.
- [ ] Add machine plan parser.
- [ ] Add schema validator.
- [ ] Add dependency graph builder.
- [ ] Add manifest loader.
- [ ] Add values loader.
- [ ] Add sample bundle fixtures.

### Epic E - Policy

- [ ] Add namespace guard.
- [ ] Add dry-run gate.
- [ ] Add approval gate.
- [ ] Add Secret detector.
- [ ] Add production data guard.
- [ ] Add command fingerprinting.
- [ ] Add redaction engine.

### Epic F - MCP clients

- [ ] Add K8s Inspector MCP client.
- [ ] Add Helm Manager MCP client.
- [ ] Add Data Ingestion MCP client.
- [ ] Add Release Note MCP client.
- [ ] Add fake MCP servers.

### Epic G - Execution engine

- [ ] Add worker loop.
- [ ] Add phase scheduler.
- [ ] Add dry-run executor.
- [ ] Add mutation executor.
- [ ] Add validation executor.
- [ ] Add rollback executor.
- [ ] Add wait executor.
- [ ] Add decision context builder.

### Epic H - Memory and reports

- [ ] Add execution memory service.
- [ ] Add semantic failure memory adapter.
- [ ] Add resource state memory.
- [ ] Add execution report generator.
- [ ] Add validation report generator.
- [ ] Add rollback report generator.
- [ ] Add release-note integration.

### Epic I - Hardening

- [ ] Add observability.
- [ ] Add deployment manifests.
- [ ] Add runbooks.
- [ ] Add sample requests.
- [ ] Run failure-injection matrix.
- [ ] Produce release candidate report.

---

## 11. Suggested Sprint Breakdown

### Sprint 1 - Skeleton and schemas

- Phase 0 and Phase 1.
- Deliver running app, docs, models, state machine.

### Sprint 2 - Persistence and bundle validation

- Phase 2 and Phase 3.
- Deliver durable jobs and valid sample bundle parsing.

### Sprint 3 - Policy gates

- Phase 4.
- Deliver hard safety guarantees before execution logic.

### Sprint 4 - APIs and MCP clients

- Phase 5 and Phase 6.
- Deliver REST/MCP job control and fake MCP contract tests.

### Sprint 5 - Dry-run runtime

- Phase 7 and Phase 8.
- Deliver dry-run-only E2E job.

### Sprint 6 - Approved mutation runtime

- Phase 9.
- Deliver approved disposable namespace execution.

### Sprint 7 - Failure loop and memory

- Phase 10 and Phase 11.
- Deliver decision-required context, external instruction resume, and memory-as-context only.

### Sprint 8 - Reports and production hardening

- Phase 12 to Phase 15.
- Deliver reports, release notes, observability, deployment, and release candidate validation.

---

## 12. CI/CD Plan

Pipeline stages:

```yaml
ci_pipeline:
  - lint
  - type_check
  - unit_tests
  - security_tests
  - policy_tests
  - mcp_contract_tests
  - integration_tests
  - e2e_dry_run_tests
  - docker_build
  - sbom_generation
  - vulnerability_scan
  - release_candidate_report
```

Merge requirements:

- lint passes;
- type check passes;
- all changed requirement IDs have tests;
- no new unredacted snapshot content;
- no skipped security tests without approval;
- docs updated when requirements change.

---

## 13. Operational Runbook Draft

### 13.1 Start service locally

```bash
uvicorn bosgenesis_mop_execution_agent.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

### 13.2 Create dry-run-only job

```bash
curl -X POST http://localhost:8080/v1/mop-executions \
  -H 'Content-Type: application/json' \
  -d '{
    "bundle_ref": {"type": "local_path", "value": "tests/fixtures/sample_mop_bundle"},
    "target_namespace": "signoz-e2e-mirror-rbac",
    "execution_mode": "dry_run_only",
    "policy_profile": "namespace-only-v1",
    "correlation_id": "local-dry-run"
  }'
```

### 13.3 Inspect job

```bash
curl http://localhost:8080/v1/mop-executions/{job_id}
```

### 13.4 Submit approval

```bash
curl -X POST http://localhost:8080/v1/mop-executions/{job_id}/approvals \
  -H 'Content-Type: application/json' \
  -d @approval.json
```

### 13.5 Submit external LLM instruction

```bash
curl -X POST http://localhost:8080/v1/mop-executions/{job_id}/instructions \
  -H 'Content-Type: application/json' \
  -d @instruction.json
```

---

## 14. Documentation Deliverables

Required before release candidate:

- `SPECS.md`;
- `PLAN.md`;
- `HLD.md`;
- `LLD.md`;
- `MACHINE_EXECUTION_PLAN_SCHEMA.md`;
- `POLICY.md`;
- `MEMORY.md`;
- `MCP_CONTRACTS.md`;
- `API.md`;
- `DEPLOYMENT.md`;
- `SAMPLE_REQUESTS.md`;
- `RUNBOOK.md`;
- `SECURITY_AND_REDACTION.md`;
- `RELEASE_CANDIDATE_REPORT.md`.

---

## 15. Risk Register

| Risk | Impact | Mitigation |
|---|---|---|
| Worker accidentally reasons or auto-remediates. | Unsafe execution. | State-machine tests, no direct LLM client in worker, explicit instruction contract, code review checklist. |
| Secret values leak into logs or memory. | Security incident. | Redaction engine, security tests, blocked Secret `.data` access, log scanning. |
| Mutation occurs without approval. | Change control violation. | Approval gate, audit-before-mutation guard, mutation tests. |
| Dry-run skipped due to plan bug. | Unsafe apply/install. | Dry-run gate independent of plan trust. |
| Namespace mismatch creates resources in wrong namespace. | Service impact. | Namespace guard and manifest normalization checks. |
| MCP timeout leaves uncertain state. | Duplicate or partial changes. | Idempotency, uncertain outcome state, external LLM decision required. |
| Restart loses state. | Broken long-running jobs. | Durable job store and restart/resume tests. |
| Concurrent jobs collide. | Race conditions. | Namespace distributed lock. |
| Historical memory becomes decision source. | Unsafe hidden reasoning. | Memory labels, tests proving memory cannot transition state. |
| Reports expose sensitive data. | Security incident. | Report redaction and snapshot tests. |

---

## 16. Definition of Ready for Implementation Tickets

A ticket is ready only when it has:

- requirement IDs;
- expected behavior;
- forbidden behavior;
- input/output schema if applicable;
- tests to add;
- policy impact;
- audit impact;
- redaction impact;
- migration impact if any;
- acceptance criteria.

---

## 17. Definition of Done for Implementation Tickets

A ticket is done only when:

- code is implemented;
- tests pass;
- requirement traceability is updated;
- policy and redaction impacts are tested;
- audit events are covered;
- state transitions are valid;
- docs are updated when behavior changes;
- no raw secrets appear in logs, fixtures, snapshots, or reports.

---

## 18. Immediate Next Actions

1. Commit `SPECS.md` and `PLAN.md`.
2. Normalize the existing HLD/LLD filenames to `HLD.md` and `LLD.md` or link them from the repository README.
3. Create project skeleton.
4. Add the sample MoP output bundle as a sanitized fixture.
5. Implement models and state machine before any MCP execution logic.
6. Implement policy/redaction before mutation logic.
7. Implement dry-run-only E2E before approved mutation.
8. Implement failure-injection tests before release candidate.
