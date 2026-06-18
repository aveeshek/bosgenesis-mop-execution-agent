"""Decision-required context packaging for external LLM controllers."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.models import ExecutionJob, ExecutionStep, Observation


class DecisionContextBuilder:
    """Package factual context without granting decision authority."""

    def build(
        self,
        *,
        job: ExecutionJob,
        reason_code: str,
        summary: str,
        step: ExecutionStep | None = None,
        observations: list[Observation] | None = None,
        allowed_next_action_types: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "target_namespace": job.target_namespace,
            "authority": "context_only_not_decision_authority",
            "reason_code": reason_code,
            "summary": summary,
            "phase_id": step.phase_id if step else job.current_phase_id,
            "step_id": step.step_id if step else job.current_step_id,
            "required_from": "external_llm",
            "allowed_next_action_types": allowed_next_action_types or [
                "continue",
                "retry",
                "wait",
                "skip",
                "abort",
            ],
            "observations": [
                observation.model_dump(mode="json") for observation in observations or []
            ],
            "memory": {
                "authority": "context_only_not_decision_authority",
                "records": [],
            },
        }
