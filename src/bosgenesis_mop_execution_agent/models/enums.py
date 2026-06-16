"""Domain enums derived from SPECS.md and OPENAPI.yaml."""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    """Job states from the OpenAPI/MCP contract."""

    CREATED = "created"
    VALIDATING_BUNDLE = "validating_bundle"
    INVALID_BUNDLE = "invalid_bundle"
    AWAITING_HUMAN_APPROVAL = "awaiting_human_approval"
    DRY_RUN_READY = "dry_run_ready"
    DRY_RUNNING = "dry_running"
    AWAITING_LLM_INSTRUCTION = "awaiting_llm_instruction"
    EXECUTING = "executing"
    DECISION_REQUIRED = "decision_required"
    PAUSED = "paused"
    WAIT_SCHEDULED = "wait_scheduled"
    VALIDATION_RUNNING = "validation_running"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    """Step states from SPECS.md."""

    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    DRY_RUN_RUNNING = "dry_run_running"
    DRY_RUN_SUCCEEDED = "dry_run_succeeded"
    DRY_RUN_FAILED = "dry_run_failed"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    MUTATION_RUNNING = "mutation_running"
    MUTATION_SUCCEEDED = "mutation_succeeded"
    MUTATION_FAILED = "mutation_failed"
    VALIDATION_RUNNING = "validation_running"
    VALIDATION_SUCCEEDED = "validation_succeeded"
    VALIDATION_FAILED = "validation_failed"
    WAITING = "waiting"
    DECISION_REQUIRED = "decision_required"
    SKIPPED_BY_INSTRUCTION = "skipped_by_instruction"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    """Execution mode from the OpenAPI contract."""

    VALIDATE_ONLY = "validate_only"
    DRY_RUN_ONLY = "dry_run_only"
    EXECUTE_AFTER_APPROVAL = "execute_after_approval"
    EXTERNAL_LLM_CONTROLLED = "external_llm_controlled"


class ApprovalStatus(StrEnum):
    """Approval status summary."""

    NOT_REQUIRED = "not_required"
    MISSING = "missing"
    ACTIVE = "active"
    EXPIRED = "expired"
    REJECTED = "rejected"


class PhaseStatus(StrEnum):
    """Execution phase status."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class StepType(StrEnum):
    """Known plan step types."""

    CONTEXT_CHECK = "context_check"
    K8S_APPLY = "k8s_apply"
    K8S_DELETE = "k8s_delete"
    K8S_GET = "k8s_get"
    K8S_VALIDATE = "k8s_validate"
    HELM_INSTALL = "helm_install"
    HELM_UPGRADE = "helm_upgrade"
    HELM_VALIDATE = "helm_validate"
    WAIT = "wait"
    MANUAL_INPUT = "manual_input"
    ROLLBACK = "rollback"
    RELEASE_NOTES = "release_notes"
    UNKNOWN = "unknown"


class ObservationSeverity(StrEnum):
    """Observation severity from the OpenAPI contract."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ObservationType(StrEnum):
    """Observation type from the OpenAPI contract."""

    STATE_TRANSITION = "state_transition"
    POLICY_CHECK = "policy_check"
    DRY_RUN_RESULT = "dry_run_result"
    MUTATION_RESULT = "mutation_result"
    VALIDATION_RESULT = "validation_result"
    MCP_CALL_RESULT = "mcp_call_result"
    WAIT_RESULT = "wait_result"
    ERROR = "error"
    DECISION_REQUEST = "decision_request"
    MEMORY_WRITE = "memory_write"


class ReportType(StrEnum):
    """Generated report artifact types."""

    EXECUTION_SUMMARY = "execution_summary"
    VALIDATION_REPORT = "validation_report"
    ROLLBACK_REPORT = "rollback_report"
    RELEASE_NOTES = "release_notes"
    AUDIT_EXPORT = "audit_export"
