"""Shared model configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictBaseModel(BaseModel):
    """Base class for domain schemas that reject accidental fields."""

    model_config = ConfigDict(extra="forbid")
