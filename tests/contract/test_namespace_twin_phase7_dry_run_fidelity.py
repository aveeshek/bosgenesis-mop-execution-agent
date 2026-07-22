from bosgenesis_mop_execution_agent.namespace_twin.dry_run_fidelity import (
    DRY_RUN_FIDELITY_VERSION,
    dry_run_fidelity_contract,
)

EXPECTED_FAILURE_MODES = {
    "image_pull_failure",
    "scheduling_failure",
    "pvc_binding_failure",
    "readiness_probe_failure",
    "controller_or_webhook_failure",
}


def test_phase7_dry_run_fidelity_demonstrates_five_post_admission_failures() -> None:
    fidelity = dry_run_fidelity_contract()

    assert fidelity["version"] == DRY_RUN_FIDELITY_VERSION
    assert fidelity["case_count"] == 5
    assert fidelity["runtime_success_predicted"] is False
    assert fidelity["runtime_validation_required"] is True
    assert {item["failure_mode"] for item in fidelity["cases"]} == EXPECTED_FAILURE_MODES

    for item in fidelity["cases"]:
        assert item["classification"] == "fidelity_limitation"
        assert item["demonstration_type"] == "deterministic_counterexample"
        assert item["runtime_success_prediction"] == "not_predicted"
        assert item["observed_in_current_run"] is False
        assert item["sequence"] == [
            {"stage": "authoritative_dry_run", "outcome": "accepted"},
            {"stage": "post_admission_runtime", "outcome": "failure_possible"},
        ]
        assert item["runtime_signal"]
        assert item["runtime_validation_required"]
        assert "runtime success remains unknown" in item["safe_conclusion"]


def test_phase7_fidelity_contract_never_claims_predicted_success() -> None:
    serialized = str(dry_run_fidelity_contract()).lower()

    assert "predicted_success" not in serialized
    assert "not_predicted" in serialized
    assert "fidelity_limitation" in serialized
