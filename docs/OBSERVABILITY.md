# BOS Genesis MoP Execution Agent Observability

This service emits structured JSON logs, Prometheus text metrics, and optional
OpenTelemetry traces for SigNoz.

## Runtime Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `LOG_FORMAT` | `json` | Set to `json` for one JSON object per log line. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `OTEL_ENABLED` | `false` | Enables OpenTelemetry tracing when `true`. |
| `OTEL_SERVICE_NAME` | `bosgenesis-mop-execution-agent` | Service name used in traces. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `SIGNOZ_ENDPOINT` | OTLP gRPC endpoint for SigNoz. |
| `OTEL_EXPORTER_OTLP_INSECURE` | `true` | Use insecure OTLP transport for in-cluster SigNoz. |
| `METRICS_ENABLED` | `true` | Documents that `/metrics` is exposed on the API port. |

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Liveness probe. Returns static process health. |
| `/readyz` | Readiness probe. Verifies repository path and reports worker/OTEL status. |
| `/metrics` | Prometheus text metrics for SigNoz or a scraper. |

## Log Fields

Every API log line includes:

- `timestamp`
- `level`
- `logger`
- `event`
- `service`
- `environment`
- `request_id`
- `job_id`
- `correlation_id`
- `trace_id`

Sensitive values are passed through the service redactor before serialization.

## Metrics

Primary metric names:

| Metric | Type | Meaning |
| --- | --- | --- |
| `bosgenesis_mop_execution_http_requests_total` | counter | API requests by method, path, status, job, correlation, and trace. |
| `bosgenesis_mop_execution_http_request_seconds` | summary | API request latency. |
| `bosgenesis_mop_execution_job_state` | gauge | Current job state marker by job and namespace. |
| `bosgenesis_mop_execution_job_state_transitions_total` | counter | Job state transitions. |
| `bosgenesis_mop_execution_job_duration_seconds` | summary | Completed job duration. |
| `bosgenesis_mop_execution_mcp_calls_total` | counter | MCP calls by server, tool, success, and error code. |
| `bosgenesis_mop_execution_mcp_latency_seconds` | summary | MCP call latency. |
| `bosgenesis_mop_execution_policy_blocks_total` | counter | Policy blocks by code. |
| `bosgenesis_mop_execution_decision_required_total` | counter | Decision-required pauses by reason code. |
| `bosgenesis_mop_execution_approval_wait_seconds` | summary | Time from job creation to human approval submission. |
| `bosgenesis_mop_execution_redactions_total` | counter | Redaction events by surface. |
| `bosgenesis_mop_execution_audit_failures_total` | counter | Failed audit writes by action. |
| `bosgenesis_mop_execution_lock_contention_total` | counter | Namespace lock contention events. |

The POC includes job, correlation, and trace IDs as labels to satisfy traceability.
For high-volume production, use relabeling or exemplars to reduce cardinality.

## SigNoz Dashboard Placeholder

Import or adapt:

`docs/dashboards/mop-execution-agent-dashboard.json`

Suggested panels:

- jobs by state
- mutation duration and completion trend
- decision-required count by reason
- policy blocks by code
- MCP latency by server/tool
- audit failure count
- namespace lock contention
- redaction volume
- API request latency and error rate

## Alerting Recommendations

Recommended alerts:

| Alert | Suggested condition | Severity |
| --- | --- | --- |
| `MopExecutionApiUnavailable` | `/healthz` or `/readyz` failing for 2 minutes | critical |
| `MopExecutionDecisionRequiredSpike` | decision-required count increases sharply over 10 minutes | warning |
| `MopExecutionAuditFailure` | any audit failure in 5 minutes | critical |
| `MopExecutionMcpOutage` | MCP error rate above 20% for 5 minutes | critical |
| `MopExecutionMcpLatencyHigh` | MCP latency p95 above operation SLO for 10 minutes | warning |
| `MopExecutionPolicyBlockSpike` | policy blocks spike above baseline | warning |
| `MopExecutionLockContention` | namespace lock contention persists for 10 minutes | warning |
| `MopExecutionLongRunningJob` | job duration exceeds configured maximum | critical |
| `MopExecutionRedactionMissing` | redaction counter is flat while reports/jobs are active | warning |

Operational rule: do not auto-repair from alerts. Alerts should send an operator
to observations, audit events, memory context, and the decision-required endpoint.
