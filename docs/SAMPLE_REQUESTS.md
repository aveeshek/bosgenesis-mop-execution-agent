# Sample Requests

## Health

```bash
curl http://localhost:8080/healthz
```

## Future Dry-Run Job Request

The dry-run job endpoint is part of the OpenAPI contract and becomes executable after the artifact ingestion and dry-run runtime phases are implemented.

```bash
curl -X POST http://localhost:8080/v1/execution-jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: local-dry-run-sample-001' \
  -d '{
    "bundle_id": "sample-bundle",
    "target_namespace": "sample-target",
    "execution_mode": "dry_run_only",
    "policy_profile": "namespace-only-v1",
    "external_llm_controller": {
      "controller_id": "codex-local"
    },
    "created_by": "operator"
  }'
```
