"""Sensitive content patterns used by policy and redaction code."""

from __future__ import annotations

import base64
import binascii
import re

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|token|api[_-]?key|secret|client_secret|private[_-]?key|"
    r"access[_-]?key|connection[_-]?string)",
    re.IGNORECASE,
)

SENSITIVE_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    (
        "connection_string",
        re.compile(
            r"\b(?:postgres(?:ql)?|mysql|mongodb|redis)://[^\s]+",
            re.IGNORECASE,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    ),
    (
        "assignment_secret",
        re.compile(
            r"\b(?:password|passwd|pwd|token|api[_-]?key|secret|client_secret)\s*[:=]\s*"
            r"[^\s,;]+",
            re.IGNORECASE,
        ),
    ),
)


def looks_like_base64_secret(value: str) -> bool:
    """Return true when a base64 value decodes to secret-like text."""
    compact = value.strip()
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return False
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return False
    try:
        decoded_text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return bool(SENSITIVE_KEY_PATTERN.search(decoded_text)) or any(
        pattern.search(decoded_text) for _, pattern in SENSITIVE_VALUE_PATTERNS
    )
