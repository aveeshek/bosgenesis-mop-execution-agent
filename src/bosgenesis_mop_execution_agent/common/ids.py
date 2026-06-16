"""Identifier helpers."""

from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    """Create a stable string identifier with a domain prefix."""
    return f"{prefix}-{uuid4()}"
