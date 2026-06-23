from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    ApprovalScope,
    AuditEvent,
    ExecutionJob,
    ExecutionStep,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    ReportType,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.reports import ReportGenerator
from bosgenesis_mop_execution_agent.runtime.rollback import (
    NAMESPACE_REVERT_RESOURCES,
    RollbackExecutor,
)
from bosgenesis_mop_execution_agent.runtime.validation import ValidationExecutor
from bosgenesis_mop_execution_agent.security import contains_sensitive_content


def test_validation_executor_checks_k8s_helm_and_custom_plan_validations() -> None:
    job = ExecutionJob(job_id="job-1", bundle_id="bundle-1", target_namespace="agent-testing")
    step = ExecutionStep(
        step_id="validate-pods",
        job_id=job.job_id,
        phase_id="validate",
        sequence_index=0,
        type=StepType.K8S_VALIDATE,
        state=StepState.VALIDATION_SUCCEEDED,
    )

    result = ValidationExecutor(
        k8s_client=FakeValidationK8sClient(),
        helm_client=FakeValidationHelmClient(),
    ).execute(job=job, steps=[step])

    assert result.success is True
    assert {check.name for check in result.checks} >= {
        "namespace_summary",
        "pods",
        "pvcs",
        "helm_releases",
        "helm_status:signoz",
        "plan_validation:validate-pods",
    }


def test_validation_executor_evaluates_response_collections_for_unhealthy_resources() -> None:
    job = ExecutionJob(job_id="job-1", bundle_id="bundle-1", target_namespace="agent-testing")

    result = ValidationExecutor(
        k8s_client=ResponseCollectionValidationK8sClient(),
        helm_client=FakeValidationHelmClient(),
    ).execute(job=job, steps=[])

    checks = {check.name: check for check in result.checks}
    assert result.success is False
    assert checks["pods"].success is False
    assert checks["pvcs"].success is False
    assert "pending-pod" in checks["pods"].summary
    assert "pending-data" in checks["pvcs"].summary

def test_rollback_executor_requires_instruction_and_approval_then_reverts_namespace() -> None:
    job = ExecutionJob(job_id="job-1", bundle_id="bundle-1", target_namespace="agent-testing")
    executor = RollbackExecutor(
        k8s_client=FakeRollbackK8sClient(),
        helm_client=FakeRollbackHelmClient(),
    )

    blocked = executor.execute(job=job, approvals=[], instructions=[])
    assert blocked.success is False
    assert blocked.warnings == [
        "rollback_approval_required",
        "external_rollback_instruction_required",
    ]

    k8s_client = FakeRollbackK8sClient()
    helm_client = FakeRollbackHelmClient()
    allowed = RollbackExecutor(k8s_client=k8s_client, helm_client=helm_client).execute(
        job=job,
        approvals=[_rollback_approval(job)],
        instructions=[_rollback_instruction(job)],
    )

    assert allowed.success is True
    assert helm_client.uninstalled == ["signoz"]
    assert k8s_client.deleted == NAMESPACE_REVERT_RESOURCES
    assert "deployments" in k8s_client.deleted
    assert "Deployment" not in k8s_client.deleted
    assert set(k8s_client.field_selectors) == {"metadata.namespace=agent-testing"}


def test_report_generator_creates_markdown_html_pdf_archive_with_redaction(tmp_path: Path) -> None:
    job = ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace="agent-testing",
        state=JobState.COMPLETED,
        correlation_id="corr-123",
        trace_id="trace-123",
    )
    step = ExecutionStep(
        step_id="apply-agent-ai-config",
        job_id=job.job_id,
        phase_id="apply",
        sequence_index=0,
        type=StepType.K8S_APPLY,
        state=StepState.MUTATION_SUCCEEDED,
        mutation_status=StepState.MUTATION_SUCCEEDED,
    )
    observation = Observation(
        observation_id="obs-1",
        job_id=job.job_id,
        severity=ObservationSeverity.INFO,
        observation_type=ObservationType.MUTATION_RESULT,
        summary="Applied manifest with password=fake-password-value",
        result={"token": "Bearer fakebearertoken1234567890"},
    )
    audit = AuditEvent(
        audit_event_id="audit-1",
        actor_type=ActorType.WORKER,
        action="mutation_pre_event",
        job_id=job.job_id,
        details={"databasePassword": "fake-password-value"},
    )

    report_set = ReportGenerator(tmp_path).generate(
        job=job,
        report_type=ReportType.CHANGE_REPORT,
        title="BOS Genesis Target Namespace Change Report",
        steps=[step],
        observations=[observation],
        audit_events=[audit],
        sections={"warnings": ["demo warning"]},
        warnings=["demo warning"],
    )
    artifact = ReportGenerator(tmp_path).artifact(
        job=job,
        report_type=ReportType.CHANGE_REPORT,
        report_set=report_set,
    )

    markdown = report_set.markdown.read_text(encoding="utf-8")
    assert report_set.html.exists()
    assert report_set.pdf.exists()
    assert report_set.archive.exists()
    pdf_bytes = report_set.pdf.read_bytes()
    assert b"xref" in pdf_bytes
    assert b"trailer" in pdf_bytes
    startxref = int(pdf_bytes.rsplit(b"startxref", 1)[1].splitlines()[1])
    assert pdf_bytes[startxref : startxref + 4] == b"xref"
    assert artifact.archive_path == str(report_set.archive)
    assert artifact.download_url == (
        f"/v1/execution-jobs/{job.job_id}/reports/{artifact.report_id}/download?artifact=pdf"
    )
    assert "trace-123" in markdown
    assert "agent-testing" in markdown
    assert not contains_sensitive_content(markdown)
    with zipfile.ZipFile(report_set.archive) as archive:
        assert set(archive.namelist()) == {
            "change-report.html",
            "change-report.md",
            "change-report.pdf",
        }


def test_api_blocks_rollback_instruction_until_rollback_is_requested() -> None:
    service = MopExecutionApiService()
    created = service.create_job({"bundle_id": "bundle-1", "target_namespace": "agent-testing"})

    response = service.submit_instruction(
        str(created["job_id"]),
        {"instruction_type": "rollback", "rationale": "too early"},
    )

    assert response["ok"] is False
    assert response["policy_blocks"][0]["code"] == "ROLLBACK_STATE_REQUIRED"


class FakeValidationK8sClient:
    def namespace_summary(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "namespace.summary", {"namespace": namespace})

    def list_pods(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "pod.list", {"result": [_pod("signoz-pod")]})

    def list_services(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "service.list", {"result": [{"name": "query"}]})

    def list_pvcs(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "pvc.list", {"result": [_pvc("data")]})

    def list_deployments(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "deployment.list", {"result": [{"name": "web"}]})

    def list_statefulsets(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "statefulset.list", {"result": []})

    def list_ingresses(self, namespace: str) -> McpCallResult:
        return _mcp_result("bosgenesis_k8s", "ingress.list", {"result": []})


class ResponseCollectionValidationK8sClient(FakeValidationK8sClient):
    def list_pods(self, namespace: str) -> McpCallResult:
        return _mcp_result(
            "bosgenesis_k8s",
            "pod.list",
            {
                "response": [
                    _pod("ready-pod"),
                    {"name": "pending-pod", "phase": "Pending", "ready": "0/1"},
                ]
            },
        )

    def list_pvcs(self, namespace: str) -> McpCallResult:
        return _mcp_result(
            "bosgenesis_k8s",
            "pvc.list",
            {"response": [_pvc("bound-data"), {"name": "pending-data", "phase": "Pending"}]},
        )

class FakeValidationHelmClient:
    def list_releases(self, *, namespace: str, all_statuses: bool = True) -> McpCallResult:
        return _mcp_result(
            "bosgenesis_helm",
            "helm.list",
            {"output": [{"name": "signoz", "status": "deployed"}]},
        )

    def status(self, *, release_name: str, namespace: str) -> McpCallResult:
        return _mcp_result(
            "bosgenesis_helm",
            "helm.status",
            {"release_name": release_name, "status": "deployed"},
        )


class FakeRollbackK8sClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.field_selectors: list[str | None] = []

    def delete_collection(
        self,
        *,
        resource: str,
        namespace: str,
        dry_run: bool = False,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> McpCallResult:
        self.deleted.append(resource)
        self.field_selectors.append(field_selector)
        return _mcp_result(
            "bosgenesis_k8s",
            f"{resource}.delete_collection",
            {"resource": resource, "namespace": namespace, "dry_run": dry_run},
        )


class FakeRollbackHelmClient:
    def __init__(self) -> None:
        self.uninstalled: list[str] = []

    def list_releases(self, *, namespace: str, all_statuses: bool = True) -> McpCallResult:
        return _mcp_result(
            "bosgenesis_helm",
            "helm.list",
            {"output": [{"name": "signoz", "status": "deployed"}]},
        )

    def rollback(
        self,
        *,
        release_name: str,
        namespace: str,
        revision: int,
        dry_run: bool = False,
    ) -> McpCallResult:
        return _mcp_result("bosgenesis_helm", "helm.rollback", {"release_name": release_name})

    def uninstall(
        self,
        *,
        release_name: str,
        namespace: str,
        dry_run: bool = False,
        keep_history: bool = False,
        force_purge_release_storage: bool = False,
    ) -> McpCallResult:
        self.uninstalled.append(release_name)
        return _mcp_result(
            "bosgenesis_helm",
            "helm.uninstall",
            {"release_name": release_name, "namespace": namespace},
        )


def _rollback_approval(job: ExecutionJob) -> HumanApproval:
    return HumanApproval(
        approval_id="approval-1",
        job_id=job.job_id,
        approver_id="demo-approver",
        approval_scope=ApprovalScope.DESTRUCTIVE_ROLLBACK,
        ticket_reference="CHG-DEMO",
        statement="Approved namespace cleanup for demo reset.",
    )


def _rollback_instruction(job: ExecutionJob) -> ExternalInstruction:
    return ExternalInstruction(
        instruction_id="instruction-rollback",
        job_id=job.job_id,
        instruction_type=InstructionType.ROLLBACK,
        controller_id="codex",
        issued_by="codex",
        rationale="Reset agent-testing after the demo.",
        destructive_action=True,
    )


def _mcp_result(server_name: str, tool_name: str, data: dict[str, Any]) -> McpCallResult:
    return McpCallResult(
        server_name=server_name,
        tool_name=tool_name,
        success=True,
        data=data,
        observation=Observation(
            observation_id=f"obs-{server_name}-{tool_name}",
            job_id="job-1",
            severity=ObservationSeverity.INFO,
            observation_type=ObservationType.MCP_CALL_RESULT,
            summary=f"{tool_name} succeeded.",
        ),
    )


def _pod(name: str) -> dict[str, str]:
    return {"name": name, "phase": "Running", "ready": "1/1"}


def _pvc(name: str) -> dict[str, str]:
    return {"name": name, "phase": "Bound"}
