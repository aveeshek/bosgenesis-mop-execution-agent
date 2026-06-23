# Runbook

## Deploy

```bash
./playbook/deploy.sh
```

Useful overrides:

```bash
NAMESPACE=bosgenesis \
IMAGE_REPOSITORY=bosgenesis-mop-execution-agent \
IMAGE_TAG=0.1.0 \
ENABLE_INGRESS=false \
SKIP_IMAGE_TRANSFER=true \
./playbook/deploy.sh
```

## Health Check

```bash
kubectl port-forward -n bosgenesis svc/bosgenesis-mop-execution-agent 8080:8080
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/metrics
```

`/healthz` is the liveness probe. `/readyz` verifies the repository backing path
and reports worker and OpenTelemetry status. `/metrics` exposes Prometheus text
metrics for SigNoz or another scraper.

## Observability

See `docs/OBSERVABILITY.md` for:

- JSON log fields and redaction behavior.
- SigNoz OpenTelemetry exporter configuration.
- Metric names for jobs, MCP calls, policy blocks, decisions, approvals,
  redactions, audit failures, and lock contention.
- Dashboard placeholder: `docs/dashboards/mop-execution-agent-dashboard.json`.
- Alerting recommendations.

## Rollback / Undeploy

```bash
./playbook/undeploy.sh
```

Set `DELETE_NAMESPACE=true` only when the namespace is dedicated to this agent.

## Safety Notes

- Keep API and worker in the same policy profile.
- Keep `MAX_PARALLEL_JOBS_PER_NAMESPACE=1`.
- Do not enable delete RBAC until destructive rollback policy is implemented and approved.
- Store credentials in Kubernetes Secrets and reference them through Helm `external.*Secret` values.
