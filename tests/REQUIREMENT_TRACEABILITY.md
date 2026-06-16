# Requirement Traceability

This file maps implementation and tests back to `knowledge-base/SPECS.md`.

| Requirement | Implementation | Tests |
|---|---|---|
| FR-038 | `src/bosgenesis_mop_execution_agent/api/app.py` health endpoint | `tests/test_health.py` |
| NFR-015 | Repository scaffold, CI, fixture, task checklist | Phase 0 placeholder tests |
| FR-001 | Pydantic domain models in `src/bosgenesis_mop_execution_agent/models/` | `tests/unit/test_phase1_models.py` |
| FR-002 | Job, phase, step, observation, instruction, approval, audit, report, resource, and policy schemas | `tests/unit/test_phase1_models.py` |
| FR-025 | Restart-rehydratable execution job schema | `tests/unit/test_phase1_models.py::test_execution_models_serialize_for_restart_rehydration` |
| FR-028 | State transitions emit observation and audit records | `tests/unit/test_phase1_state_machine.py::test_allowed_transition_returns_auditable_records` |
| NFR-001 | Restart-safe model serialization baseline | `tests/unit/test_phase1_models.py::test_execution_models_serialize_for_restart_rehydration` |
| NFR-002 | Deterministic state transition table and guard hooks | `tests/unit/test_phase1_state_machine.py` |
| NFR-008 | Deterministic error codes and problem details mapping | `tests/unit/test_phase1_models.py::test_problem_details_mapping_uses_deterministic_error_code` |
| NFR-013 | Correlation ID and trace ID fields on domain models | `tests/unit/test_phase1_models.py` |
| AC-021 | Restart-loadable state baseline | `tests/unit/test_phase1_models.py::test_execution_models_serialize_for_restart_rehydration` |
| FR-025 | PostgreSQL migration DDL and durable JSON repository | `tests/unit/test_phase2_persistence.py` |
| FR-026 | Append-only audit writer | `tests/unit/test_phase2_persistence.py::test_append_only_audit_writer_rejects_duplicate_event_id` |
| FR-030 | Idempotency key storage, replay, and conflict behavior | `tests/unit/test_phase2_persistence.py::test_idempotency_store_replays_same_request_and_blocks_conflict` |
| FR-031 | Redis-style namespace lock and heartbeat services | `tests/unit/test_phase2_locks.py` |
| NFR-001 | Repository and idempotency records rehydrate after restart | `tests/unit/test_phase2_persistence.py` |
| NFR-003 | Audit event store rejects duplicate event IDs and has no update/delete path | `tests/unit/test_phase2_persistence.py::test_append_only_audit_writer_rejects_duplicate_event_id` |
| NFR-006 | Namespace lock service enforces one active owner | `tests/unit/test_phase2_locks.py::test_namespace_lock_prevents_concurrent_owners` |
| AC-022 | Append-only audit behavior | `tests/unit/test_phase2_persistence.py::test_append_only_audit_writer_rejects_duplicate_event_id` |
| AC-024 | Namespace lock contention behavior | `tests/unit/test_phase2_locks.py::test_namespace_lock_prevents_concurrent_owners` |
| NFR-012 | Docker runtime entrypoints, Helm values, playbook scripts, and deployment docs | `tests/unit/test_phase14_deployment_assets.py` |
| NFR-006 | Helm chart RBAC, NetworkPolicy, resources, scaling, worker concurrency, and namespace lock settings | `tests/unit/test_phase14_deployment_assets.py` |
