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
