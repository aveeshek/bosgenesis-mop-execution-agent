from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.observability.logging import JsonLogFormatter
from bosgenesis_mop_execution_agent.observability.metrics import METRICS


def test_metrics_endpoint_and_request_correlation_headers() -> None:
    METRICS.reset()
    client = TestClient(create_app())

    response = client.get(
        "/readyz",
        headers={"X-Correlation-ID": "corr-observe", "X-Trace-ID": "trace-observe"},
    )
    metrics = client.get("/metrics").text

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "corr-observe"
    assert response.headers["X-Trace-ID"] == "trace-observe"
    assert "bosgenesis_mop_execution_http_requests_total" in metrics
    assert 'correlation_id="corr-observe"' in metrics
    assert 'trace_id="trace-observe"' in metrics


def test_job_state_metrics_include_job_correlation_and_trace_ids() -> None:
    METRICS.reset()
    client = TestClient(create_app())

    created = client.post(
        "/v1/execution-jobs",
        json={
            "job_id": "job-observability",
            "bundle_id": "bundle-observability",
            "target_namespace": "agent-testing",
            "correlation_id": "corr-job-observability",
            "trace_id": "trace-job-observability",
        },
    ).json()
    metrics = client.get("/metrics").text

    assert created["ok"] is True
    assert "bosgenesis_mop_execution_job_state" in metrics
    assert 'job_id="job-observability"' in metrics
    assert 'correlation_id="corr-job-observability"' in metrics
    assert 'trace_id="trace-job-observability"' in metrics


def test_json_logging_redacts_sensitive_extra_fields() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        "test",
        logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="structured_event",
        args=(),
        exc_info=None,
    )
    record.bosgenesis = {
        "event": "unit_test",
        "job_id": "job-log",
        "password": "super-secret-value",
    }

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "unit_test"
    assert payload["job_id"] == "job-log"
    assert payload["password"] == "[REDACTED]"
