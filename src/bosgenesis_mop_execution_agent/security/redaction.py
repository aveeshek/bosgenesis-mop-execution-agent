"""Deterministic redaction for logs, JSON/YAML payloads, reports, and memory writes."""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.security.sensitive_patterns import (
    SENSITIVE_KEY_PATTERN,
    SENSITIVE_VALUE_PATTERNS,
    looks_like_base64_secret,
)

REDACTION_TEXT = "[REDACTED]"

# These Kubernetes fields reference Secret objects or service-account token
# projection settings; they do not contain secret values themselves. Nested
# strings are still inspected for sensitive value patterns.
SAFE_SECRET_REFERENCE_KEYS = frozenset(
    {
        "automountserviceaccounttoken",
        "imagepullsecrets",
        "secretkeyref",
        "secretref",
        "serviceaccounttoken",
    }
)


class SensitiveFinding(StrictBaseModel):
    """Location and reason for a sensitive content finding."""

    path: str
    kind: str
    excerpt: str = Field(default=REDACTION_TEXT)


def contains_sensitive_content(value: Any) -> bool:
    """Return true if a value contains secret-like content."""
    return bool(find_sensitive_content(value))


def find_sensitive_content(value: Any, path: str = "$") -> list[SensitiveFinding]:
    """Find sensitive keys or values in nested JSON-compatible content."""
    findings: list[SensitiveFinding] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            nested_path = f"{path}.{key_text}"
            if _is_sensitive_key(key_text) and nested not in (
                None,
                "",
                [],
                {},
                REDACTION_TEXT,
            ):
                findings.append(SensitiveFinding(path=nested_path, kind="sensitive_key"))
            findings.extend(find_sensitive_content(nested, nested_path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(find_sensitive_content(nested, f"{path}[{index}]"))
    elif isinstance(value, str):
        findings.extend(_string_findings(value, path))
    return findings


def redact_value(value: Any) -> Any:
    """Recursively redact secret-like fields and values from JSON/YAML-compatible content."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text) and nested not in (None, "", [], {}):
                redacted[key_text] = REDACTION_TEXT
            else:
                redacted[key_text] = redact_value(nested)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


def redact_string(value: str) -> str:
    """Redact secret-like substrings from plain text."""
    redacted = value
    for _, pattern in SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTION_TEXT, redacted)
    redacted = _redact_base64_secret_tokens(redacted)
    return redacted


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return (
        bool(SENSITIVE_KEY_PATTERN.search(value)) and normalized not in SAFE_SECRET_REFERENCE_KEYS
    )


def _string_findings(value: str, path: str) -> list[SensitiveFinding]:
    findings = [
        SensitiveFinding(path=path, kind=kind)
        for kind, pattern in SENSITIVE_VALUE_PATTERNS
        if pattern.search(value)
    ]
    if any(looks_like_base64_secret(token) for token in _candidate_tokens(value)):
        findings.append(SensitiveFinding(path=path, kind="base64_secret"))
    return findings


def _redact_base64_secret_tokens(value: str) -> str:
    redacted = value
    for token in _candidate_tokens(value):
        if looks_like_base64_secret(token):
            redacted = redacted.replace(token, REDACTION_TEXT)
    return redacted


def _candidate_tokens(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9+/=]{16,}", value)
