---
name: bosgenesis-mop-execution-agent
description: Use when Codex needs to control, validate, rollback, report on, or inspect BOS Genesis MoP execution jobs through the `bosgenesis_mop_execution` MCP server, including bundle registration, job lifecycle, external instructions, approvals, observations, audit, memory context, policy evaluation, namespace revert, validation reports, rollback reports, change reports, release notes, and report download metadata.
---

# BOS Genesis MoP Execution Agent

Use the `bosgenesis_mop_execution` MCP server when working with downstream execution of MoP Creation Agent artifact bundles. The server URL is expected to be configured in Codex as:

```toml
[mcp_servers.bosgenesis_mop_execution]
url = "http://mop-execution-agent.bosgenesis.local/mcp"
```

## Authority Model

- Treat `machine_execution_plan.yaml` as the canonical execution contract.
- Treat human MoP Markdown and installation notes as supporting context only.
- Never infer remediation or mutate resources from reasoning alone.
- Use observations, audit events, policy blocks, and `next_required_decision` as facts.
- Guardrails override plans, instructions, approvals, memory, and user convenience.

## Safe Workflow

1. Call `mop_execution_health` before using the server.
2. Call `mop_execution_get_capabilities` to confirm supported tools and guardrails.
3. Register or validate bundles with `mop_execution_register_bundle` and `mop_execution_validate_bundle`.
4. Create jobs with `mop_execution_create_job`.
5. Start execution with `mop_execution_start_job` only after confirming target namespace and bundle validation.
6. When a job pauses in `decision_required`, call `mop_execution_get_next_required_decision`, inspect observations/audit events, then submit a bounded instruction with `mop_execution_submit_instruction`.
7. Submit human approval references with `mop_execution_submit_approval` only when the approval scope, job ID, target namespace, phase, step, and command fingerprint match.
8. Use `mop_execution_list_observations` and `mop_execution_list_audit_events` for factual status. Do not invent missing state.
9. Run post-execution validation with `mop_execution_run_validation` after dry-run or mutation completes.
10. Request rollback with `mop_execution_request_rollback` only with explicit instruction and approval context.
11. Execute rollback or namespace revert only through `mop_execution_execute_rollback` or `mop_execution_revert_namespace` after the user explicitly approves cleanup.
12. Generate final execution, validation, rollback, change, and release-note reports after the execution has reached a reportable state.
13. Use `mop_execution_download_report` for download metadata and then fetch the REST `download_url`; do not ask MCP to stream PDF bytes.

## Tool Routing

Read-only or discovery tools:

- `mop_execution_health`
- `mop_execution_get_capabilities`
- `mop_execution_get_job`
- `mop_execution_list_jobs`
- `mop_execution_get_plan`
- `mop_execution_get_next_required_decision`
- `mop_execution_list_observations`
- `mop_execution_list_audit_events`
- `mop_execution_get_memory_context`
- `mop_execution_evaluate_policy`
- `mop_execution_download_report`

Bundle and job lifecycle tools:

- `mop_execution_register_bundle`
- `mop_execution_validate_bundle`
- `mop_execution_create_job`
- `mop_execution_start_job`
- `mop_execution_pause_job`
- `mop_execution_resume_job`
- `mop_execution_cancel_job`
- `mop_execution_run_validation`

Controller and approval tools:

- `mop_execution_submit_instruction`
- `mop_execution_submit_approval`
- `mop_execution_request_rollback`
- `mop_execution_execute_rollback`
- `mop_execution_revert_namespace`

Report tools:

- `mop_execution_generate_execution_report`
- `mop_execution_generate_validation_report`
- `mop_execution_generate_rollback_report`
- `mop_execution_generate_change_report`
- `mop_execution_generate_release_notes`
- `mop_execution_download_report`

Report download workflow:

1. Generate or list the report and capture `report_id`.
2. Call `mop_execution_download_report` with `job_id`, `report_id`, and `artifact` (`markdown`, `html`, `pdf`, or `archive`).
3. Use the returned REST URL:

```text
/v1/execution-jobs/{job_id}/reports/{report_id}/download?artifact=pdf
```

4. Download binary files through REST only. MCP returns metadata/link envelopes, not PDF bytes.
5. If a PDF is generated after the ReportLab PDF fix, prefer newly generated files over older downloaded files.

## Guardrails

- Do not continue after `policy_blocks` unless the block has an explicit allowed resolution path.
- Do not submit mutating instructions without dry-run evidence and human approval.
- Do not submit approvals that are expired, unscoped, namespace-mismatched, or missing command fingerprints.
- Do not execute rollback or namespace revert without explicit user confirmation.
- Do not copy, request, store, or reveal Kubernetes Secret values.
- Do not request production data or PVC content copy.
- Keep all summaries redacted. If a tool response includes `redaction_applied = false`, treat it as unsafe and stop.

## Expected Failure Handling

When an MCP call fails, use the returned observation and structured error as the source of truth. Do not reason around an MCP outage, timeout, malformed response, policy block, or missing approval. Pause, report the exact failure, and ask for the next explicit instruction when required.
