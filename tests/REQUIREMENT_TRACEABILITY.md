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
| FR-006 | Bundle source resolver and canonical machine plan loading | `tests/unit/test_phase3_bundle_validation.py` |
| FR-007 | Versioned `machine_execution_plan.yaml` parser | `tests/unit/test_phase3_bundle_validation.py::test_unsupported_schema_fails_closed` |
| FR-008 | Installation notes fallback and human MoP supporting context only | `tests/unit/test_phase3_bundle_validation.py::test_embedded_installation_notes_plan_is_fallback` |
| FR-009 | Generated Kubernetes manifest loading and validation | `tests/unit/test_phase3_bundle_validation.py` |
| FR-010 | Helm values file safety validation | `tests/unit/test_phase3_bundle_validation.py::test_values_file_sensitive_key_fails_closed` |
| FR-011 | Optional artifact metadata loading | `tests/unit/test_phase3_bundle_validation.py::test_sample_bundle_parses_machine_plan_first_and_loads_supporting_context` |
| FR-012 | Dependency graph validation and cycle detection | `tests/unit/test_phase3_bundle_validation.py::test_dependency_cycle_fails_closed` |
| AC-001 | Machine plan precedence over Markdown context | `tests/unit/test_phase3_bundle_validation.py::test_sample_bundle_parses_machine_plan_first_and_loads_supporting_context` |
| AC-002 | Fail-closed invalid bundle validation | `tests/unit/test_phase3_bundle_validation.py` |
| AC-003 | Fail-closed unknown references and unsafe resources | `tests/unit/test_phase3_bundle_validation.py` |
| FR-015 | Dry-run gate for mutating actions | `tests/unit/test_phase4_policy_engine.py::test_mutating_action_requires_dry_run_approval_idempotency_and_audit` |
| FR-016 | Human approval gate for mutating actions | `tests/unit/test_phase4_policy_engine.py` |
| FR-017 | Approval scope, command fingerprint, expiration, and resource matching | `tests/unit/test_phase4_policy_engine.py` |
| FR-018 | Namespace scope and cluster-scoped resource policy | `tests/unit/test_phase4_policy_engine.py::test_namespace_scope_and_cluster_scope_are_blocked` |
| FR-019 | Secret value detection across manifests, values, instructions, logs, and outputs | `tests/unit/test_phase4_policy_engine.py::test_secret_values_are_detected_across_payload_types` |
| FR-020 | Production data and PVC content copy blocker | `tests/unit/test_phase4_policy_engine.py::test_production_data_and_pvc_copy_are_blocked` |
| FR-030 | Idempotency guard integrated with policy decisions | `tests/unit/test_phase4_policy_engine.py::test_timeout_retry_and_idempotency_mismatch_are_blocked` |
| FR-032 | Timeout and retry limit policy checks | `tests/unit/test_phase4_policy_engine.py::test_timeout_retry_and_idempotency_mismatch_are_blocked` |
| FR-033 | Redaction for strings and structured payloads | `tests/security/test_phase4_redaction.py` |
| AC-005 | Mutation without dry-run is blocked | `tests/unit/test_phase4_policy_engine.py::test_mutating_action_requires_dry_run_approval_idempotency_and_audit` |
| AC-006 | Mutation without approval is blocked | `tests/unit/test_phase4_policy_engine.py::test_mutating_action_requires_dry_run_approval_idempotency_and_audit` |
| AC-007 | Approval scope mismatch is blocked | `tests/unit/test_phase4_policy_engine.py::test_approval_scope_mismatch_and_expiration_are_blocked` |
| AC-008 | Resource outside target namespace is blocked | `tests/unit/test_phase4_policy_engine.py::test_namespace_scope_and_cluster_scope_are_blocked` |
| AC-009 | Cluster-scoped resource is blocked | `tests/unit/test_phase4_policy_engine.py::test_namespace_scope_and_cluster_scope_are_blocked` |
| AC-010 | Secret values are detected, redacted, and blocked | `tests/unit/test_phase4_policy_engine.py::test_secret_values_are_detected_across_payload_types` |
| AC-023 | Audit-before-mutation guard blocks unaudited mutation | `tests/unit/test_phase4_policy_engine.py::test_mutating_action_requires_dry_run_approval_idempotency_and_audit` |
| AC-028 | Raw secret values are absent after redaction | `tests/security/test_phase4_redaction.py` |
| FR-021 | Kubernetes Inspector MCP typed client methods and failure observations | `tests/contract/test_phase6_mcp_clients.py` |
| FR-022 | Helm Manager MCP typed client methods and failure observations | `tests/contract/test_phase6_mcp_clients.py` |
| FR-023 | Data Ingestion MCP typed client methods | `tests/contract/test_phase6_mcp_clients.py::test_helm_data_ingestion_and_release_note_clients_expose_typed_methods` |
| FR-024 | Release Note MCP typed client methods | `tests/contract/test_phase6_mcp_clients.py::test_helm_data_ingestion_and_release_note_clients_expose_typed_methods` |
| NFR-011 | MCP timeout, retry, redaction, structured errors, observations, and audit hooks | `tests/contract/test_phase6_mcp_clients.py` |
| FR-001 | REST artifact bundle registration/validation and execution job creation endpoints | `tests/contract/test_phase5_rest_api.py` |
| FR-002 | REST job state control, retrieval, plan, observation, event, audit, memory, and report endpoints | `tests/contract/test_phase5_rest_api.py` |
| FR-003 | External instruction submission endpoint | `tests/contract/test_phase5_rest_api.py::test_instruction_approval_reports_cancel_and_mcp_mirror` |
| FR-004 | Human approval submission endpoint | `tests/contract/test_phase5_rest_api.py::test_instruction_approval_reports_cancel_and_mcp_mirror` |
| FR-005 | Start, pause, resume, cancel, and rollback request endpoints | `tests/contract/test_phase5_rest_api.py` |
| FR-037 | MCP server tools mirror REST job-control behavior and return standard result envelopes | `tests/test_mcp_server.py` |
| FR-038 | Health, readiness, capabilities, effective redacted config, and auth placeholder | `tests/contract/test_phase5_rest_api.py` |
| FR-039 | Report list, metadata, and release note request endpoints | `tests/contract/test_phase5_rest_api.py::test_instruction_approval_reports_cancel_and_mcp_mirror` |
| FR-013 | Phase dependency scheduler and next step selector | `tests/unit/test_phase7_runtime.py::test_scheduler_respects_phase_and_step_dependencies` |
| FR-014 | Runtime ambiguity and unsupported execution pause as decision-required without worker reasoning | `tests/unit/test_phase7_runtime.py::test_wait_timeout_enters_decision_required_without_reasoning` |
| FR-025 | Restart-safe queue and worker loop over persisted jobs | `tests/unit/test_phase7_runtime.py::test_worker_restart_rehydrates_runnable_jobs` |
| FR-028 | Runtime observations for state transitions, dry-run results, waits, errors, and decisions | `tests/unit/test_phase7_runtime.py` |
| FR-029 | Decision-required context builder with context-only memory authority | `tests/unit/test_phase7_runtime.py::test_namespace_lock_contention_pauses_for_decision` |
| FR-031 | Runtime namespace lock acquisition and release | `tests/unit/test_phase7_runtime.py::test_cancel_safe_stop_releases_lock_and_cancels_pending_steps` |
| FR-032 | Wait/poll timeout handling | `tests/unit/test_phase7_runtime.py::test_wait_timeout_enters_decision_required_without_reasoning` |
| AC-011 | Runtime failures stop in decision-required state without remediation | `tests/unit/test_phase7_runtime.py::test_wait_timeout_enters_decision_required_without_reasoning` |
| AC-012 | Long-running waits and timeouts emit decision context | `tests/unit/test_phase7_runtime.py::test_wait_timeout_enters_decision_required_without_reasoning` |
| AC-013 | Restart recovery requeues persisted runnable state | `tests/unit/test_phase7_runtime.py::test_worker_restart_rehydrates_runnable_jobs` |
| AC-018 | Cancellations and decision-required states are restart-safe and auditable | `tests/unit/test_phase7_runtime.py` |
| AC-021 | Worker restart recovery uses persisted job state | `tests/unit/test_phase7_runtime.py::test_worker_restart_rehydrates_runnable_jobs` |
| AC-024 | Namespace lock contention pauses execution and keeps the existing lock owner | `tests/unit/test_phase7_runtime.py::test_namespace_lock_contention_pauses_for_decision` |
| FR-015 | Plan command kinds map to deterministic Kubernetes and Helm dry-run actions | `tests/unit/test_phase8_dry_run.py` |
| FR-021 | Kubernetes server-side dry-run apply executor | `tests/unit/test_phase8_dry_run.py::test_dry_run_only_e2e_uses_kubernetes_server_side_dry_run_sample_bundle` |
| FR-022 | Helm template and dry-run install/upgrade executor | `tests/unit/test_phase8_dry_run.py::test_helm_render_failure_fixture_pauses_before_dry_run_install` |
| FR-028 | Dry-run outputs are persisted as redacted observations | `tests/unit/test_phase8_dry_run.py::test_dry_run_only_e2e_uses_kubernetes_server_side_dry_run_sample_bundle` |
| FR-040 | `dry_run_only` jobs complete preflight without mutation | `tests/unit/test_phase8_dry_run.py::test_dry_run_only_e2e_uses_kubernetes_server_side_dry_run_sample_bundle` |
| AC-004 | Dry-run-only E2E uses the sample MoP bundle | `tests/unit/test_phase8_dry_run.py::test_dry_run_only_e2e_uses_kubernetes_server_side_dry_run_sample_bundle` |
| AC-012 | YAML syntax errors pause without MCP mutation or worker reasoning | `tests/unit/test_phase8_dry_run.py::test_yaml_syntax_error_fixture_pauses_before_mcp_call` |
| AC-015 | Helm render failures pause before dry-run install/upgrade mutation path | `tests/unit/test_phase8_dry_run.py::test_helm_render_failure_fixture_pauses_before_dry_run_install` |
| FR-016 | Mutation gate requires matching active human approval before namespace-scoped mutation | `tests/unit/test_phase9_mutation.py` |
| FR-017 | Mutation approval matching uses bounded step scope and command fingerprint | `tests/unit/test_phase9_mutation.py::test_mutation_gates_block_missing_dry_run_approval_scope_and_namespace` |
| FR-018 | Mutation gate blocks manifests outside the target namespace | `tests/unit/test_phase9_mutation.py::test_mutation_gates_block_missing_dry_run_approval_scope_and_namespace` |
| FR-021 | Kubernetes apply executor runs only after mutation gates pass | `tests/unit/test_phase9_mutation.py::test_approved_disposable_namespace_k8s_mutation_executes_after_all_gates` |
| FR-022 | Helm install/upgrade executor runs only after mutation gates pass | `tests/unit/test_phase9_mutation.py::test_approved_helm_install_upgrade_mutation_uses_helm_executor` |
| FR-028 | Mutation observations include redacted outputs and resource mutation records | `tests/unit/test_phase9_mutation.py::test_approved_disposable_namespace_k8s_mutation_executes_after_all_gates` |
| FR-030 | Duplicate continue instruction idempotency replays the same bounded request | `tests/unit/test_phase9_mutation.py::test_duplicate_continue_instruction_idempotency_replays_same_request` |
| NFR-001 | Unknown mutation outcome pauses with a critical factual observation | `tests/unit/test_phase9_mutation.py::test_unknown_mutation_outcome_pauses_with_critical_observation` |
| AC-019 | Mutations require explicit external continue instruction and approved scope | `tests/unit/test_phase9_mutation.py::test_mutation_cannot_occur_without_continue_instruction` |
| AC-020 | Approved disposable namespace integration path uses fake MCP mutation clients | `tests/unit/test_phase9_mutation.py::test_approved_disposable_namespace_k8s_mutation_executes_after_all_gates` |
