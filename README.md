# BOS Genesis MoP Execution Agent

Deterministic, externally controlled execution worker for BOS Genesis MoP artifact bundles.

The worker is intentionally not a reasoning agent. It parses explicit plans, enforces guardrails, records observations and audit events, and waits for external LLM instructions or human approval whenever reasoning or authorization is required.

## Development

```bash
python -m pytest
python -m ruff check .
python -m mypy src tests
uvicorn bosgenesis_mop_execution_agent.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

Canonical design documents currently live in `knowledge-base/` and are linked from root-level Markdown files for convenience.

## Namespace Twin demo scoring

The deterministic rule set is version `namespace-twin-risk-1.2.0`. Risk bands are 0-30 low/Green, 31-70 medium/Amber, 71-90 high/Red, and 91-100 critical/Red.

Demo feature toggles:

~~~properties
NAMESPACE_TWIN_PVC_RISK_ENABLED=false
NAMESPACE_TWIN_STATEFULSET_RISK_ENABLED=false
~~~

These toggles remove the corresponding numeric contributions only. They do not suppress policy hard blocks, evidence gaps, dry-run failures, or execution approval. PVC and StatefulSet scoring must be re-enabled only after their production evidence requirements are implemented and calibrated.