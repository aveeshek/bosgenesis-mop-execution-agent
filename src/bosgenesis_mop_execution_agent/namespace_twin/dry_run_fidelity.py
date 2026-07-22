"""Deterministic counterexamples for Kubernetes dry-run fidelity."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DRY_RUN_FIDELITY_VERSION = "1.0.0"

_FIDELITY_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "image_pull_after_admission",
        "failure_mode": "image_pull_failure",
        "title": "Image pull can fail after dry-run acceptance",
        "runtime_signal": "ImagePullBackOff or ErrImagePull",
        "why_not_proven": (
            "API admission does not pull the image from its registry or prove image, "
            "credential, architecture, and network availability on a target node."
        ),
        "runtime_validation_required": (
            "Observe Pod container waiting reasons and image-pull events after creation."
        ),
    },
    {
        "case_id": "scheduling_after_admission",
        "failure_mode": "scheduling_failure",
        "title": "Scheduling can fail after dry-run acceptance",
        "runtime_signal": "FailedScheduling with a Pending Pod",
        "why_not_proven": (
            "API admission does not reserve live node capacity or prove affinity, taints, "
            "topology, quota, and scheduler constraints can be satisfied at runtime."
        ),
        "runtime_validation_required": (
            "Observe Pod scheduling conditions and scheduler events after creation."
        ),
    },
    {
        "case_id": "pvc_binding_after_admission",
        "failure_mode": "pvc_binding_failure",
        "title": "PVC binding can fail after dry-run acceptance",
        "runtime_signal": "Pending PVC or FailedBinding event",
        "why_not_proven": (
            "API admission does not provision or bind storage and cannot prove a matching "
            "StorageClass, provisioner, topology, capacity, or access mode is available."
        ),
        "runtime_validation_required": (
            "Observe PVC phase, binding events, StorageClass, and provisioner outcome."
        ),
    },
    {
        "case_id": "readiness_probe_after_admission",
        "failure_mode": "readiness_probe_failure",
        "title": "Readiness probes can fail after dry-run acceptance",
        "runtime_signal": "Unhealthy event or Ready=False condition",
        "why_not_proven": (
            "API admission validates probe structure but does not start the workload, call "
            "the probe endpoint, or prove application dependencies become healthy."
        ),
        "runtime_validation_required": (
            "Observe readiness conditions, probe events, endpoint state, and safe pod logs."
        ),
    },
    {
        "case_id": "controller_webhook_after_admission",
        "failure_mode": "controller_or_webhook_failure",
        "title": "Controllers or webhooks can fail after dry-run acceptance",
        "runtime_signal": "Reconcile error, webhook timeout, or rejected dependent resource",
        "why_not_proven": (
            "A successful admission response does not prove asynchronous controller "
            "reconciliation, later webhook calls, generated resources, or external control "
            "plane dependencies will converge."
        ),
        "runtime_validation_required": (
            "Observe controller conditions, events, webhook health, and generated resources."
        ),
    },
)


def dry_run_fidelity_contract() -> dict[str, Any]:
    """Return immutable counterexamples without presenting them as observed failures."""
    cases: list[dict[str, Any]] = []
    for item in _FIDELITY_CASES:
        cases.append(
            {
                **deepcopy(item),
                "classification": "fidelity_limitation",
                "demonstration_type": "deterministic_counterexample",
                "sequence": [
                    {"stage": "authoritative_dry_run", "outcome": "accepted"},
                    {"stage": "post_admission_runtime", "outcome": "failure_possible"},
                ],
                "runtime_success_prediction": "not_predicted",
                "observed_in_current_run": False,
                "safe_conclusion": (
                    "Dry-run acceptance proves admission/static validation only; runtime "
                    "success remains unknown until post-creation evidence is collected."
                ),
            }
        )
    return {
        "version": DRY_RUN_FIDELITY_VERSION,
        "classification": "fidelity_limitation",
        "dry_run_scope": "api_admission_and_static_validation",
        "runtime_success_predicted": False,
        "runtime_validation_required": True,
        "case_count": len(cases),
        "summary": (
            "A successful dry-run is not a prediction of runtime success. The cases below "
            "are deterministic counterexamples, not failures observed in the current run."
        ),
        "cases": cases,
    }
