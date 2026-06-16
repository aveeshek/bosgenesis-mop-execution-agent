# Deployment

## Helm

```bash
NAMESPACE=bosgenesis \
IMAGE_REPOSITORY=ghcr.io/aveeshek/bosgenesis-mop-execution-agent \
IMAGE_TAG=0.1.0 \
./playbook/deploy.sh
```

The chart deploys:

- API deployment and service.
- Optional worker deployment.
- Optional reconciler deployment.
- Optional migration hook job.
- ConfigMap for non-secret runtime configuration.
- External secret references for database, Redis, ClickHouse, Qdrant, and Langfuse credentials.
- Namespace-scoped Role/RoleBinding.
- Optional PVC, ingress, and NetworkPolicy.

## Migration Job

The PostgreSQL schema lives in `migrations/postgres/0001_phase2_core.sql`. The Helm migration hook is disabled by default until the platform migration runner is wired to execute that SQL against the configured `DATABASE_URL`.

## Runtime Configuration

Important values:

- `config.maxParallelJobsPerNamespace`: expected to remain `1` for namespace mutation safety.
- `config.namespaceLockLeaseSeconds`: Redis namespace lock lease duration.
- `worker.enabled`: enables the async worker once Phase 7 is implemented.
- `reconciler.enabled`: enables recovery once reconciler logic is implemented.
- `rbac.allowDelete`: must remain `false` unless rollback/destructive flows are approved.
