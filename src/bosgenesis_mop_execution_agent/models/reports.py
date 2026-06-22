"""Report artifact models."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.enums import ReportType


class ReportArtifact(StrictBaseModel):
    """Generated report or archive metadata."""

    report_id: str
    report_type: ReportType
    path: str
    job_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    download_url: str | None = None
    html_path: str | None = None
    pdf_path: str | None = None
    archive_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    redacted: bool = True
