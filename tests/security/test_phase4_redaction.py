from __future__ import annotations

from bosgenesis_mop_execution_agent.security import (
    REDACTION_TEXT,
    contains_sensitive_content,
    redact_string,
    redact_value,
)


def test_redacts_sensitive_strings_without_emitting_raw_values() -> None:
    raw_password = "fake-password-value"
    raw_token = "fakebearertoken1234567890"
    raw_connection = "postgres://user:pass@example.com:5432/app"
    raw_private_key = "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"
    text = (
        f"password={raw_password} Authorization: Bearer {raw_token} "
        f"db={raw_connection} key={raw_private_key}"
    )

    redacted = redact_string(text)

    assert REDACTION_TEXT in redacted
    assert raw_password not in redacted
    assert raw_token not in redacted
    assert raw_connection not in redacted
    assert raw_private_key not in redacted


def test_redacts_yaml_json_values_logs_events_reports_and_memory_payloads() -> None:
    payload = {
        "yaml": {"databasePassword": "fake-password-value"},
        "json": {"token": "fake-token-value"},
        "log": "api_key=fake-api-key",
        "event": {"connection_string": "redis://:secret@example.com:6379/0"},
        "report": ["Bearer fakebearertoken1234567890"],
        "memory": {"PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----"},
        "safe": "hello",
    }

    redacted = redact_value(payload)

    assert contains_sensitive_content(payload)
    assert not contains_sensitive_content(redacted)
    assert redacted["safe"] == "hello"
    assert "fake-password-value" not in str(redacted)
    assert "fake-token-value" not in str(redacted)
    assert "fake-api-key" not in str(redacted)
    assert "redis://:secret@example.com:6379/0" not in str(redacted)
