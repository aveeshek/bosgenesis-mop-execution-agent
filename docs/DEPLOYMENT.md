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

The PostgreSQL schema lives in:

- `migrations/postgres/0001_phase2_core.sql`
- `migrations/postgres/0002_phase11_memory.sql`

The Helm migration hook is disabled by default until the platform migration runner is wired to execute those SQL files against the configured `POSTGRES_DSN`. `DATABASE_URL` is still accepted as a backward-compatible alias, but new deployments should follow the MoP Creation Agent convention: `POSTGRES_ENABLED=true`, `POSTGRES_SCHEMA=mop_execution`, and `POSTGRES_DSN` from a Kubernetes Secret.

## Runtime Configuration

Important values:

- `config.maxParallelJobsPerNamespace`: expected to remain `1` for namespace mutation safety.
- `config.namespaceLockLeaseSeconds`: Redis namespace lock lease duration.
- `config.memoryEnabled`: defaults to `true`; memory is execution context only.
- `config.memoryPostgresEnabled`: defaults to `true`; durable memory records use PostgreSQL.
- `config.postgresSchema`: defaults to `mop_execution`.
- `config.logFormat`: defaults to `json` for structured logs.
- `config.enableOtel`: enables OpenTelemetry traces to SigNoz.
- `config.signozEndpoint`: OTLP gRPC endpoint used when tracing is enabled.
- `config.metricsEnabled`: documents that `/metrics` is available on the API port.
- `worker.enabled`: enables the async worker once Phase 7 is implemented.
- `reconciler.enabled`: enables recovery once reconciler logic is implemented.
- `rbac.allowDelete`: must remain `false` unless rollback/destructive flows are approved.
