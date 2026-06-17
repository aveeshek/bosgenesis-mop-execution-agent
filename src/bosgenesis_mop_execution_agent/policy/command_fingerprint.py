"""Stable command fingerprinting."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def command_fingerprint(command: str, metadata: dict[str, Any] | None = None) -> str:
    """Return a stable SHA-256 fingerprint for a bounded command."""
    payload = {"command": command, "metadata": metadata or {}}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
