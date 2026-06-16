# BOS Genesis MoP Execution Agent - MCP Tool Contract

**Document status:** Draft v0.1  
**MCP server name:** `bosgenesis-mop-execution-agent`  
**Alternative service name:** `bosgenesis-k8s-mop-installer-agent`  
**Primary mode:** Async, long-running, externally controlled execution worker  
**Reasoning posture:** No autonomous reasoning authority  
**Execution posture:** Deterministic worker with strict mechanical guardrails  
**Primary client:** External LLM controller, Codex, BOS AI Studio, n8n, or another orchestration layer

---

## 1. Purpose

This document defines the MCP tool contract for the MoP Execution Agent.

The MCP server exposes tools that allow an external LLM controller to register a MoP output bundle, validate it, create an execution job, start dry-run or execution phases, submit next-action instructions, submit human approvals, inspect observations, retrieve audit events, and request reports.

The MCP server is intentionally **not** a reasoning server. It does not infer remediation. It does not decide whether an error is safe to ignore. It does not independently patch manifests, reorder execution, skip resources, retry failed steps, or roll back resources.

```text
The external LLM is the brain.
The MoP Execution Agent is the hands, memory, safety boundary, and audit trail.
```

```text
Memory is allowed to remember.
Memory is not allowed to decide.
```

---

## 2. Authority Model

| Actor | Authority |
|---|---|
| External LLM controller | Decides next step, retry strategy, patch strategy, wait strategy, validation request, skip/continue, rollback strategy, or abort. |
| Human approver | Authorizes mutating, destructive, rollback, namespace-creation, or policy-exception actions. |
| MoP Execution Agent | Enforces schema, namespace scope, dry-run gate, approval gate, idempotency, redaction, locks, timeouts, audit writes, and deterministic policy. |
| Memory | Supplies factual prior context only. No decision authority. |

Guardrails override every MCP request, external LLM instruction, human approval, memory record, and artifact.

---

## 3. Source-of-Truth Order

When the worker needs to understand the bundle, it uses this order:

1. `machine_execution_plan.yaml`
2. Embedded `machine_execution_plan` block in installation notes
3. Generated YAML and Helm values files
4. `artifact.json`
5. `artifact-index.json`
6. Human-readable MoP Markdown
7. Professional MoP PDF, for human review only
8. External LLM instruction
9. Memory context, as factual prior context only

Guardrail policy has higher authority than all of the above.

---

## 4. Standard Tool Result Envelope

Every MCP tool returns a result envelope with the following shape unless the tool-specific output says otherwise.

```json
{
  "ok": true,
  "correlation_id": "corr-123",
  "job_id": "job-123",
  "bundle_id": "bundle-123",
  "state": "decision_required",
  "message": "Dry-run failed. External LLM instruction required.",
  "data": {},
  "observations": [],
  "policy_blocks": [],
  "next_required_decision": null,
  "redaction_applied": true,
  "next_poll_after_seconds": 5
}
```

### Result envelope fields

| Field | Required | Meaning |
|---|---:|---|
| `ok` | Yes | Whether the tool request itself was accepted. Policy-blocked requests may return `ok=false` with policy facts. |
| `correlation_id` | No | Caller-supplied or worker-generated correlation ID. |
| `job_id` | No | Execution job ID when relevant. |
| `bundle_id` | No | Artifact bundle ID when relevant. |
| `state` | No | Current job state. |
| `message` | Yes | Human-readable summary. |
| `data` | Yes | Tool-specific result payload. |
| `observations` | No | Redacted factual observations created by the tool call. |
| `policy_blocks` | No | Deterministic guardrail blocks or warnings. |
| `next_required_decision` | No | External LLM or human decision needed before progress can continue. |
| `redaction_applied` | Yes | Whether redaction was applied to output. |
| `next_poll_after_seconds` | No | Suggested polling delay for async operations. |

---

## 5. Job States

```yaml
job_states:
  - created
  - validating_bundle
  - invalid_bundle
  - awaiting_human_approval
  - dry_run_ready
  - dry_running
  - awaiting_llm_instruction
  - executing
  - decision_required
  - paused
  - wait_scheduled
  - validation_running
  - rollback_requested
  - rolling_back
  - completed
  - failed
  - cancelled
```

---

## 6. Allowed External Instruction Types

```yaml
instruction_types:
  continue:
    meaning: Continue to the next mechanically eligible step.
    requires_worker_reasoning: false
  retry:
    meaning: Retry an explicit failed step or MCP call using explicit retry bounds.
    requires_worker_reasoning: false
  wait:
    meaning: Wait a specified duration, then observe again.
    requires_worker_reasoning: false
  skip:
    meaning: Skip an explicit step after external LLM decision and required approvals.
    requires_worker_reasoning: false
  patch_manifest:
    meaning: Apply an explicit patch supplied by the external LLM controller.
    requires_worker_reasoning: false
  replace_manifest:
    meaning: Replace an explicit manifest with supplied redacted content.
    requires_worker_reasoning: false
  run_validation:
    meaning: Run an explicit validation command or MCP validation action.
    requires_worker_reasoning: false
  request_human_approval:
    meaning: Move job to approval-required state with an explicit approval scope.
    requires_worker_reasoning: false
  rollback:
    meaning: Execute explicit rollback instruction after required approval.
    requires_worker_reasoning: false
  abort:
    meaning: Stop the job without rollback.
    requires_worker_reasoning: false
  refresh_observation:
    meaning: Re-read explicit resource state, logs, events, or Helm status.
    requires_worker_reasoning: false
  invoke_mcp_tool:
    meaning: Invoke an explicit governed MCP operation after policy validation.
    requires_worker_reasoning: false
```

---

## 7. MCP Tools

### 7.1 `mop_execution_health`

Returns liveness and basic service metadata.

**Input schema**

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["service", "version", "status", "timestamp"],
  "properties": {
    "service": {"type": "string"},
    "version": {"type": "string"},
    "status": {"type": "string", "enum": ["ok"]},
    "timestamp": {"type": "string", "format": "date-time"}
  }
}
```

---

### 7.2 `mop_execution_get_capabilities`

Returns supported guardrails, states, instruction types, MCP adapters, and memory layers.

**Input schema**

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["reasoning_authority", "guardrails", "instruction_types", "mcp_adapters"],
  "properties": {
    "reasoning_authority": {
      "type": "object",
      "properties": {
        "worker_agent": {"const": false},
        "external_llm_controller": {"const": true},
        "human_approver": {"const": "authorization_only"}
      }
    },
    "guardrails": {"type": "array", "items": {"type": "string"}},
    "instruction_types": {"type": "array", "items": {"type": "string"}},
    "mcp_adapters": {"type": "array", "items": {"type": "object"}}
  }
}
```

---

### 7.3 `mop_execution_register_bundle`

Registers a MoP Creation Agent output bundle by path, archive reference, object storage URI, or upstream MoP run reference.

The tool does not execute anything.

**Input schema**

```json
{
  "type": "object",
  "required": ["source", "target_namespace"],
  "properties": {
    "source": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {
          "type": "string",
          "enum": ["local_path", "object_storage_uri", "mop_creation_run_ref", "archive_upload_ref", "inline_index"]
        },
        "uri": {"type": "string"},
        "local_path": {"type": "string"},
        "mop_id": {"type": "string"},
        "run_id": {"type": "string"},
        "upload_id": {"type": "string"},
        "artifact_index": {"type": "object"}
      },
      "additionalProperties": false
    },
    "target_namespace": {"type": "string"},
    "expected_mop_id": {"type": "string"},
    "expected_run_id": {"type": "string"},
    "registered_by": {"type": "string"},
    "correlation_id": {"type": "string"},
    "labels": {"type": "object", "additionalProperties": {"type": "string"}}
  },
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["bundle_id", "status", "target_namespace"],
  "properties": {
    "bundle_id": {"type": "string"},
    "status": {"type": "string", "enum": ["registered", "uploaded", "unpacked", "validated", "invalid", "quarantined"]},
    "mop_id": {"type": "string"},
    "run_id": {"type": "string"},
    "source_namespace": {"type": "string"},
    "target_namespace": {"type": "string"},
    "files": {"type": "array", "items": {"type": "object"}}
  }
}
```

---

### 7.4 `mop_execution_validate_bundle`

Validates bundle structure, required files, plan consistency, target namespace scope, and redaction posture.

**Input schema**

```json
{
  "type": "object",
  "required": ["bundle_id"],
  "properties": {
    "bundle_id": {"type": "string"},
    "target_namespace": {"type": "string"},
    "strict": {"type": "boolean", "default": true},
    "require_machine_execution_plan": {"type": "boolean", "default": true},
    "require_no_secret_values": {"type": "boolean", "default": true},
    "require_target_namespace_only": {"type": "boolean", "default": true},
    "correlation_id": {"type": "string"}
  },
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["bundle_id", "valid", "summary", "findings"],
  "properties": {
    "bundle_id": {"type": "string"},
    "valid": {"type": "boolean"},
    "summary": {"type": "object"},
    "findings": {"type": "array", "items": {"type": "object"}}
  }
}
```

---

### 7.5 `mop_execution_create_job`

Creates an async execution job from a validated or validatable artifact bundle.

The job may parse and validate immediately, but it must not mutate Kubernetes, Helm, or application systems during job creation.

**Input schema**

```json
{
  "type": "object",
  "required": ["bundle_id", "target_namespace", "execution_mode", "external_llm_controller"],
  "properties": {
    "bundle_id": {"type": "string"},
    "job_name": {"type": "string"},
    "target_namespace": {"type": "string"},
    "source_namespace": {"type": "string"},
    "execution_mode": {
      "type": "string",
      "enum": ["validate_only", "dry_run_only", "execute_after_approval", "external_llm_controlled"]
    },
    "policy_profile": {"type": "string", "default": "namespace-only-v1"},
    "external_llm_controller": {
      "type": "object",
      "required": ["controller_id"],
      "properties": {
        "controller_id": {"type": "string"},
        "callback_url": {"type": "string", "format": "uri"},
        "decision_timeout_seconds": {"type": "integer", "minimum": 1},
        "context_pack_preference": {
          "type": "string",
          "enum": ["minimal", "standard", "full_redacted"]
        }
      }
    },
    "approval_policy": {
      "type": "object",
      "properties": {
        "human_approval_before_mutation": {"type": "boolean", "default": true},
        "human_approval_before_rollback": {"type": "boolean", "default": true},
        "human_approval_before_destructive_action": {"type": "boolean", "default": true},
        "dry_run_before_mutation": {"type": "boolean", "default": true}
      }
    },
    "created_by": {"type": "string"},
    "correlation_id": {"type": "string"},
    "idempotency_key": {"type": "string"},
    "labels": {"type": "object", "additionalProperties": {"type": "string"}}
  },
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["job_id", "state", "target_namespace"],
  "properties": {
    "job_id": {"type": "string"},
    "bundle_id": {"type": "string"},
    "state": {"type": "string"},
    "target_namespace": {"type": "string"},
    "current_phase_id": {"type": "string"},
    "current_step_id": {"type": "string"},
    "dry_run_satisfied": {"type": "boolean"},
    "decision_required": {"type": "boolean"}
  }
}
```

---

### 7.6 `mop_execution_get_job`

Retrieves current job state, progress, gates, and next required decision.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "include_next_required_decision": {"type": "boolean", "default": true}
  },
  "additionalProperties": false
}
```

---

### 7.7 `mop_execution_list_jobs`

Lists jobs with optional filters.

**Input schema**

```json
{
  "type": "object",
  "properties": {
    "state": {"type": "string"},
    "target_namespace": {"type": "string"},
    "correlation_id": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    "cursor": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.8 `mop_execution_start_job`

Starts or continues a validated job until the next pause condition.

This tool may perform non-mutating checks and dry-runs. It must stop before mutation unless dry-run and approval gates are satisfied.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "max_steps_until_pause": {"type": "integer", "minimum": 1},
    "allow_non_mutating_checks": {"type": "boolean", "default": true},
    "require_decision_before_first_mutation": {"type": "boolean", "default": true},
    "requested_by": {"type": "string"},
    "correlation_id": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.9 `mop_execution_pause_job`

Requests a safe pause.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "requested_by", "reason"],
  "properties": {
    "job_id": {"type": "string"},
    "requested_by": {"type": "string"},
    "reason": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.10 `mop_execution_resume_job`

Resumes a paused job. This tool does not add new reasoning or remediation.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "requested_by"],
  "properties": {
    "job_id": {"type": "string"},
    "requested_by": {"type": "string"},
    "expected_state": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.11 `mop_execution_cancel_job`

Cancels a job. Cancellation does not roll back already-applied resources.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "requested_by", "reason", "acknowledge_no_automatic_rollback"],
  "properties": {
    "job_id": {"type": "string"},
    "requested_by": {"type": "string"},
    "reason": {"type": "string"},
    "acknowledge_no_automatic_rollback": {"type": "boolean", "const": true}
  },
  "additionalProperties": false
}
```

---

### 7.12 `mop_execution_submit_instruction`

Submits an explicit external LLM instruction.

The worker checks the instruction mechanically and either executes it, schedules it, records it, or blocks it with a policy observation.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "instruction_id", "instruction_type", "controller_id", "issued_by"],
  "properties": {
    "job_id": {"type": "string"},
    "instruction_id": {"type": "string"},
    "controller_id": {"type": "string"},
    "issued_by": {"type": "string"},
    "issued_at": {"type": "string", "format": "date-time"},
    "instruction_type": {
      "type": "string",
      "enum": [
        "continue",
        "retry",
        "wait",
        "skip",
        "patch_manifest",
        "replace_manifest",
        "run_validation",
        "request_human_approval",
        "rollback",
        "abort",
        "refresh_observation",
        "invoke_mcp_tool"
      ]
    },
    "target_phase_id": {"type": "string"},
    "target_step_id": {"type": "string"},
    "target_resource": {"$ref": "#/definitions/resource_ref"},
    "rationale": {
      "type": "string",
      "description": "Non-authoritative explanation. The worker does not infer actions from this field."
    },
    "wait_seconds": {"type": "integer", "minimum": 1},
    "retry_policy": {
      "type": "object",
      "properties": {
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        "backoff_seconds": {"type": "integer", "minimum": 0}
      }
    },
    "manifest_patch": {
      "type": "object",
      "properties": {
        "patch_type": {"type": "string", "enum": ["json_patch", "merge_patch", "strategic_merge_patch"]},
        "patch": {"type": "object"},
        "target_file": {"type": "string"},
        "target_resource": {"$ref": "#/definitions/resource_ref"}
      }
    },
    "replacement_manifest": {"type": "string"},
    "validation_selector": {"type": "string"},
    "mcp_server": {"type": "string"},
    "mcp_tool": {"type": "string"},
    "mcp_arguments": {"type": "object"},
    "approval_token": {"type": "string"},
    "dry_run_required": {"type": "boolean", "default": true},
    "destructive_action": {"type": "boolean", "default": false},
    "safety_acknowledgements": {"type": "array", "items": {"type": "string"}},
    "correlation_id": {"type": "string"},
    "idempotency_key": {"type": "string"}
  },
  "additionalProperties": false,
  "definitions": {
    "resource_ref": {
      "type": "object",
      "properties": {
        "api_version": {"type": "string"},
        "kind": {"type": "string"},
        "namespace": {"type": "string"},
        "name": {"type": "string"},
        "file_path": {"type": "string"},
        "helm_release_name": {"type": "string"}
      }
    }
  }
}
```

---

### 7.13 `mop_execution_submit_approval`

Records a human approval with a bounded scope.

Approval does not bypass guardrails. Approval does not permit Secret value copying, production data copying, out-of-namespace actions, or unmanaged destructive behavior.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "approver_id", "approval_scope", "ticket_reference", "statement"],
  "properties": {
    "job_id": {"type": "string"},
    "approver_id": {"type": "string"},
    "approver_role": {"type": "string"},
    "approval_scope": {
      "type": "string",
      "enum": [
        "non_mutating_validation",
        "dry_run",
        "namespace_creation",
        "mutation",
        "rollback",
        "destructive_rollback",
        "policy_exception"
      ]
    },
    "ticket_reference": {"type": "string"},
    "statement": {"type": "string"},
    "expires_at": {"type": "string", "format": "date-time"},
    "approved_resource_refs": {"type": "array", "items": {"type": "object"}},
    "approved_phase_ids": {"type": "array", "items": {"type": "string"}},
    "approved_step_ids": {"type": "array", "items": {"type": "string"}},
    "policy_exception_id": {"type": "string"},
    "correlation_id": {"type": "string"},
    "idempotency_key": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.14 `mop_execution_get_plan`

Returns the parsed `machine_execution_plan.yaml` and normalized execution graph.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "include_raw_machine_plan": {"type": "boolean", "default": false}
  },
  "additionalProperties": false
}
```

---

### 7.15 `mop_execution_get_next_required_decision`

Returns the current decision request for the external LLM controller or human approver.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "include_context_pack": {"type": "boolean", "default": true},
    "context_pack_preference": {
      "type": "string",
      "enum": ["minimal", "standard", "full_redacted"],
      "default": "standard"
    }
  },
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["decision_required"],
  "properties": {
    "decision_required": {"type": "boolean"},
    "decision_id": {"type": "string"},
    "required_from": {"type": "string", "enum": ["external_llm", "human_approver", "both"]},
    "reason_code": {"type": "string"},
    "prompt_summary": {"type": "string"},
    "allowed_instruction_types": {"type": "array", "items": {"type": "string"}},
    "context_pack": {"type": "object"}
  }
}
```

---

### 7.16 `mop_execution_list_observations`

Lists redacted observations for a job.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "phase_id": {"type": "string"},
    "step_id": {"type": "string"},
    "severity": {"type": "string", "enum": ["debug", "info", "warning", "error", "critical"]},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    "cursor": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.17 `mop_execution_list_audit_events`

Lists immutable audit events for a job.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "actor_type": {"type": "string", "enum": ["worker", "external_llm", "human", "system", "mcp_server"]},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    "cursor": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.18 `mop_execution_get_memory_context`

Returns redacted execution memory context for an external LLM.

Memory is context only and must be labeled as non-authoritative.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {"type": "string"},
    "include_semantic_failure_memory": {"type": "boolean", "default": true},
    "include_audit_summary": {"type": "boolean", "default": true},
    "max_records_per_layer": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
  },
  "additionalProperties": false
}
```

**Output `data` authority field**

```json
{
  "authority": "context_only_not_decision_authority",
  "layers": [
    {
      "name": "episodic_execution_memory",
      "reasoning_allowed": false,
      "records": []
    }
  ]
}
```

---

### 7.19 `mop_execution_evaluate_policy`

Evaluates a proposed action without executing it.

**Input schema**

```json
{
  "type": "object",
  "required": ["action", "target_namespace"],
  "properties": {
    "job_id": {"type": "string"},
    "action": {"type": "string"},
    "target_namespace": {"type": "string"},
    "source_namespace": {"type": "string"},
    "mutating": {"type": "boolean", "default": false},
    "destructive": {"type": "boolean", "default": false},
    "dry_run_satisfied": {"type": "boolean", "default": false},
    "approval_token": {"type": "string"},
    "resource_refs": {"type": "array", "items": {"type": "object"}},
    "manifest_content": {"type": "string"},
    "instruction": {"type": "object"}
  },
  "additionalProperties": false
}
```

**Output `data` schema**

```json
{
  "type": "object",
  "required": ["allowed", "blocks", "warnings"],
  "properties": {
    "allowed": {"type": "boolean"},
    "blocks": {"type": "array", "items": {"type": "object"}},
    "warnings": {"type": "array", "items": {"type": "object"}}
  }
}
```

---

### 7.20 `mop_execution_request_rollback`

Requests rollback using an explicit external LLM rollback instruction and required approval.

The worker does not decide rollback strategy.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "requested_by", "instruction"],
  "properties": {
    "job_id": {"type": "string"},
    "requested_by": {"type": "string"},
    "instruction": {"type": "object"},
    "approval_token": {"type": "string"},
    "rollback_scope": {
      "type": "string",
      "enum": ["current_step", "current_phase", "entire_job", "explicit_resources"]
    },
    "resource_refs": {"type": "array", "items": {"type": "object"}},
    "correlation_id": {"type": "string"},
    "idempotency_key": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

### 7.21 `mop_execution_generate_release_notes`

Requests execution notes or release notes after completion, failure, or rollback.

This tool delegates writing to the release-note agent where configured. It does not alter cluster state.

**Input schema**

```json
{
  "type": "object",
  "required": ["job_id", "requested_by"],
  "properties": {
    "job_id": {"type": "string"},
    "requested_by": {"type": "string"},
    "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
    "include_audit_summary": {"type": "boolean", "default": true},
    "include_redacted_observations": {"type": "boolean", "default": true},
    "correlation_id": {"type": "string"}
  },
  "additionalProperties": false
}
```

---

## 8. MCP Resources

The server may expose these read-only MCP resources for clients that support resource discovery.

| Resource URI | Purpose |
|---|---|
| `mop-execution://capabilities` | Server capability document. |
| `mop-execution://bundles/{bundle_id}` | Bundle metadata and validation summary. |
| `mop-execution://jobs/{job_id}` | Current job state. |
| `mop-execution://jobs/{job_id}/plan` | Parsed execution plan. |
| `mop-execution://jobs/{job_id}/observations` | Redacted observations. |
| `mop-execution://jobs/{job_id}/audit` | Immutable audit events. |
| `mop-execution://jobs/{job_id}/memory-context` | Redacted, non-authoritative execution memory context. |
| `mop-execution://jobs/{job_id}/reports` | Generated reports and release notes. |

---

## 9. Guardrail Contract

The worker must block or pause on these conditions:

| Code | Condition | Behavior |
|---|---|---|
| `TARGET_NAMESPACE_VIOLATION` | Action targets a namespace outside the job target namespace. | Block and emit policy observation. |
| `CLUSTER_SCOPED_RESOURCE_BLOCKED` | Action attempts cluster-scoped mutation in v1. | Block unless a future policy profile explicitly allows it. |
| `SECRET_VALUE_DETECTED` | Input or output appears to contain raw secret material. | Redact and block if the secret would be persisted or applied. |
| `PRODUCTION_DATA_COPY_BLOCKED` | Action attempts to copy database rows, documents, Kafka messages, Redis values, files, or business payloads. | Block. |
| `DRY_RUN_REQUIRED` | Mutating action requested without successful prior dry-run. | Pause and request dry-run or approval path. |
| `HUMAN_APPROVAL_REQUIRED` | Mutating or destructive action requested without valid human approval. | Pause and request approval. |
| `IDEMPOTENCY_LOCK_CONFLICT` | Another active operation owns the same job/namespace/resource lock. | Pause or fail based on timeout policy. |
| `LLM_REASONING_REQUIRED` | Error requires interpretation or remediation. | Move to `decision_required`. |
| `TIMEOUT_EXCEEDED` | Tool execution exceeded configured timeout. | Pause and emit observation. |

---

## 10. MCP Adapter Boundary

The execution agent uses other MCP servers as controlled hands:

| External MCP server | Expected use |
|---|---|
| `bosgenesis-helm-manager-mcp` | Helm repo add/update, template, dry-run install/upgrade, install/upgrade, status, history, rollback, uninstall. |
| `bosgenesis-k8s-inspector-agent` | Namespace get/create, server-side dry-run, apply, get, describe/events/logs, wait, validation. |
| `bosgenesis-k8s-data-ingestion-agent` | Historical and current namespace snapshots from PostgreSQL/ClickHouse-backed ingestion. |
| `bosgenesis-mop-creation-agent` | Retrieve or regenerate MoP bundles. |
| `bosgenesis-release-note-agent` | Generate execution notes and release notes from redacted observations and audit summaries. |

The execution agent should not bypass these MCPs with raw shell commands unless the deployment profile explicitly enables shell mode for local development.

---

## 11. Example Flow

```text
1. External LLM calls mop_execution_register_bundle.
2. External LLM calls mop_execution_validate_bundle.
3. External LLM calls mop_execution_create_job.
4. External LLM calls mop_execution_start_job.
5. Worker performs parsing, checks, and dry-run-safe actions.
6. Worker reaches awaiting_human_approval or decision_required.
7. External LLM calls mop_execution_get_next_required_decision.
8. Human calls mop_execution_submit_approval when mutation is authorized.
9. External LLM calls mop_execution_submit_instruction with continue/retry/patch/wait/etc.
10. Worker executes mechanically through governed MCPs.
11. Worker records observations, audit events, memory records, and reports.
12. External LLM calls mop_execution_generate_release_notes.
```

---

## 12. Error Model

All errors must be returned as redacted, structured observations and result envelope fields. The worker must avoid raw stack traces in user-facing MCP outputs.

```json
{
  "ok": false,
  "message": "Policy blocked instruction.",
  "policy_blocks": [
    {
      "code": "DRY_RUN_REQUIRED",
      "message": "Mutating action cannot proceed until dry-run succeeds.",
      "guardrail": "dry_run_before_mutation"
    }
  ],
  "redaction_applied": true,
  "next_required_decision": {
    "required_from": "external_llm",
    "reason_code": "dry_run_required",
    "allowed_instruction_types": ["run_validation", "continue", "abort"]
  }
}
```

---

## 13. Acceptance Requirements

1. Every mutating instruction is blocked unless dry-run and approval gates are satisfied.
2. Every policy block emits an audit event and observation.
3. Every MCP call is correlated to job ID, phase ID, step ID, and instruction ID where available.
4. Every tool output is redacted.
5. Memory context is always labeled `context_only_not_decision_authority`.
6. The worker never invents next steps or remediation.
7. The worker never stores raw Secret values.
8. The worker never copies production data.
9. The worker only targets the job target namespace in v1.
10. The worker supports restart/resume using durable job state.
