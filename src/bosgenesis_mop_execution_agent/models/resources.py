"""Resource reference models."""

from __future__ import annotations

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class ResourceRef(StrictBaseModel):
    """Reference to a Kubernetes resource or Helm release."""

    api_version: str | None = None
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None
    file_path: str | None = None
    helm_release_name: str | None = None
