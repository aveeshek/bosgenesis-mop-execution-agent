"""Operator-ready execution, validation, rollback, and change reports."""

from __future__ import annotations

import html
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models import (
    AuditEvent,
    ExecutionJob,
    ExecutionStep,
    Observation,
    ReportArtifact,
    ReportType,
)
from bosgenesis_mop_execution_agent.security import redact_value


@dataclass(frozen=True)
class GeneratedReportSet:
    markdown: Path
    html: Path
    pdf: Path
    archive: Path


class ReportGenerator:
    """Create consistent operator-facing reports in Markdown, HTML, PDF, and zip."""

    def __init__(self, artifact_root: str | Path) -> None:
        self._artifact_root = Path(artifact_root)

    def generate(
        self,
        *,
        job: ExecutionJob,
        report_type: ReportType,
        title: str,
        steps: list[ExecutionStep],
        observations: list[Observation],
        audit_events: list[AuditEvent],
        sections: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> GeneratedReportSet:
        report_dir = self._artifact_root / "reports" / job.job_id
        report_dir.mkdir(parents=True, exist_ok=True)
        slug = report_type.value.replace("_", "-")
        context = _context(
            job=job,
            title=title,
            report_type=report_type,
            steps=steps,
            observations=observations,
            audit_events=audit_events,
            sections=sections or {},
            warnings=warnings or [],
        )
        markdown = report_dir / f"{slug}.md"
        html_path = report_dir / f"{slug}.html"
        pdf = report_dir / f"{slug}.pdf"
        archive = report_dir / f"{slug}.zip"
        markdown.write_text(_markdown(context), encoding="utf-8")
        html_path.write_text(_html(context), encoding="utf-8")
        _write_pdf(pdf, context)
        with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for path in (markdown, html_path, pdf):
                zip_file.write(path, arcname=path.name)
        return GeneratedReportSet(markdown=markdown, html=html_path, pdf=pdf, archive=archive)

    def artifact(
        self,
        *,
        job: ExecutionJob,
        report_type: ReportType,
        report_set: GeneratedReportSet,
    ) -> ReportArtifact:
        report_id = f"report-{report_type.value}-{job.job_id}"
        return ReportArtifact(
            report_id=report_id,
            report_type=report_type,
            path=str(report_set.markdown),
            job_id=job.job_id,
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
            download_url=(
                f"/v1/execution-jobs/{job.job_id}/reports/{report_id}/download?artifact=pdf"
            ),
            archive_path=str(report_set.archive),
            html_path=str(report_set.html),
            pdf_path=str(report_set.pdf),
            redacted=True,
        )


def _context(
    *,
    job: ExecutionJob,
    title: str,
    report_type: ReportType,
    steps: list[ExecutionStep],
    observations: list[Observation],
    audit_events: list[AuditEvent],
    sections: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    mutating_steps = [
        step
        for step in steps
        if step.mutation_status is not None or step.state.value.startswith("mutation")
    ]
    validation_steps = [
        step
        for step in steps
        if step.validation_status is not None or "validate" in step.type.value
    ]
    return redact_value(
        {
            "title": title,
            "report_type": report_type.value,
            "generated_at": utc_now().isoformat(),
            "job": job.model_dump(mode="json"),
            "summary": {
                "target_namespace": job.target_namespace,
                "state": job.state.value,
                "correlation_id": job.correlation_id,
                "trace_id": job.trace_id,
                "total_steps": job.progress.total_steps,
                "completed_steps": job.progress.completed_steps,
                "failed_steps": job.progress.failed_steps,
                "mutation_step_count": len(mutating_steps),
                "validation_step_count": len(validation_steps),
                "observation_count": len(observations),
                "audit_event_count": len(audit_events),
            },
            "warnings": warnings,
            "sections": sections,
            "steps": [step.model_dump(mode="json") for step in steps],
            "observations": [item.model_dump(mode="json") for item in observations],
            "audit_events": [item.model_dump(mode="json") for item in audit_events],
        }
    )


def _markdown(context: dict[str, Any]) -> str:
    summary = context["summary"]
    lines = [
        f"# {context['title']}",
        "",
        f"Generated: `{context['generated_at']}`",
        f"Report type: `{context['report_type']}`",
        "",
        "## Executive Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Target namespace | `{summary['target_namespace']}` |",
        f"| Job state | `{summary['state']}` |",
        f"| Correlation ID | `{summary.get('correlation_id') or ''}` |",
        f"| Trace ID | `{summary.get('trace_id') or ''}` |",
        f"| Steps | `{summary['completed_steps']}/{summary['total_steps']}` |",
        f"| Failed steps | `{summary['failed_steps']}` |",
        f"| Observations | `{summary['observation_count']}` |",
        f"| Audit events | `{summary['audit_event_count']}` |",
        "",
        "## Warnings",
        "",
    ]
    warnings = context.get("warnings") or []
    lines.extend([f"- {warning}" for warning in warnings] or ["- None"])
    lines.extend(["", "## Namespace Changes", "", _step_table(context.get("steps") or [])])
    for name, value in (context.get("sections") or {}).items():
        lines.extend(["", f"## {_title(str(name))}", "", _section_value(value)])
    lines.extend(
        [
            "",
            "## Observation Trace",
            "",
            _observation_table(context.get("observations") or []),
        ]
    )
    return "\n".join(lines) + "\n"


def _html(context: dict[str, Any]) -> str:
    summary = context["summary"]
    sections = context.get("sections") or {}
    warning_chips = "".join(
        f"<span class='chip warn'>{html.escape(str(warning))}</span>"
        for warning in (context.get("warnings") or ["None"])
    )
    gate_rows = _policy_gate_rows(context)
    mutation_rows = _mutation_rows(context)
    validation_rows = _validation_rows(context)
    resource_rows = _resource_change_rows(context)
    source_namespace = html.escape(str(sections.get("source_namespace") or ""))
    correlation_id = html.escape(str(summary.get("correlation_id") or ""))
    gate_table = (
        _html_table(gate_rows, ["severity", "step_id", "codes", "summary"])
        if gate_rows
        else "<p class='callout'>No blocking policy gates remained at completion.</p>"
    )
    validation_table = (
        _html_table(validation_rows, ["check", "status", "summary"])
        if validation_rows
        else "<p class='callout'>No validation checks were recorded in this report.</p>"
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(str(context["title"]))}</title>
  <style>
    :root {{
      --primary:#0d2e52; --secondary:#006e9e; --accent:#00a1b8;
      --success:#007a54; --warning:#f38c1f; --danger:#b81f2e;
      --ink:#1a242e; --muted:#63737f; --line:#c2d6e0; --paper:#eef5f8;
      --panel:#fbfdff; --brand:#e20074;
    }}
    @page {{ size: letter; margin: 0.55in; }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; font-family: Arial, Helvetica, sans-serif; font-size:12px;
      line-height:1.45; color:var(--ink); background:var(--paper);
    }}
    .cover {{
      min-height:11in; color:white; padding:52px 58px;
      background:
        radial-gradient(circle at 86% 12%, rgba(0,161,184,.34) 0 88px, transparent 90px),
        radial-gradient(circle at 76% 88%, rgba(0,110,158,.42) 0 120px, transparent 122px),
        linear-gradient(rgba(69,128,161,.22) 1px, transparent 1px),
        linear-gradient(90deg, rgba(69,128,161,.22) 1px, transparent 1px),
        var(--primary);
      background-size:auto, auto, 32px 32px, 32px 32px, auto;
      page-break-after: always;
      position:relative;
    }}
    .cover:after {{
      content:""; position:absolute; left:0; right:0; bottom:0; height:108px;
      background:var(--secondary); border-top:10px solid var(--accent);
    }}
    .eyebrow {{ color:var(--brand); font-weight:700; text-transform:uppercase; }}
    .cover .eyebrow {{ color:#85f2ff; font-size:13px; }}
    .cover h1 {{ margin:22px 0 12px; max-width:680px; font-size:40px; line-height:1.08; }}
    .cover .subtitle {{ color:#ddf7fb; max-width:680px; font-size:15px; }}
    .cover .detail-grid {{
      margin-top:46px; display:grid; grid-template-columns: 130px minmax(0,1fr);
      gap:10px 16px; max-width:720px; font-size:12px; position:relative; z-index:1;
    }}
    .cover .label {{ color:#96e8f2; font-weight:700; }}
    .page {{ background:#fff; padding:34px 42px 44px; page-break-after:always; }}
    .page-header {{
      margin:-34px -42px 28px; padding:15px 42px 13px;
      color:white; background:var(--primary); border-bottom:6px solid var(--accent);
      display:flex; justify-content:space-between; gap:16px; align-items:center;
    }}
    h1,h2,h3 {{ margin:0; line-height:1.2; }}
    h2 {{ color:var(--primary); font-size:24px; margin:0 0 8px; }}
    h3 {{ color:var(--primary); font-size:15px; margin:22px 0 8px; }}
    .intro {{ color:#28456d; border-bottom:3px solid var(--accent); padding-bottom:10px; }}
    .grid {{
      display:grid; grid-template-columns: repeat(4, minmax(0,1fr));
      gap:12px; margin:18px 0 24px;
    }}
    .metric {{
      background:white; border:1px solid var(--line);
      border-top:6px solid var(--accent); padding:12px; min-height:70px;
    }}
    .metric b {{ display:block; color:var(--primary); font-size:22px; margin-top:5px; }}
    table {{
      width:100%; border-collapse:collapse; background:white; margin:12px 0 24px;
      table-layout:fixed;
    }}
    th,td {{
      border:1px solid var(--line); padding:9px 10px;
      text-align:left; vertical-align:top; font-size:11px; overflow-wrap:anywhere;
    }}
    th {{ background:#eaf7fa; color:#0d2e52; font-weight:700; }}
    tr:nth-child(even) td {{ background:#f8fbfd; }}
    .chip {{
      display:inline-block; padding:5px 8px; border-radius:999px;
      background:#edf7ed; margin:2px 4px 2px 0; font-size:12px;
    }}
    .warn {{ background:#fff4db; color:#7c4d00; }}
    .callout {{
      border-left:5px solid var(--accent); background:#f4fbfd; padding:12px 14px;
      margin:14px 0 20px;
    }}
    .muted {{ color:var(--muted); }}
  </style>
</head>
<body>
  <section class="cover">
    <div class="eyebrow">BOS GENESIS</div>
    <h1>{html.escape(str(context["title"]))}</h1>
    <div class="subtitle">Operator evidence report generated by the MoP Execution Agent.</div>
    <div class="grid" style="max-width:720px;margin-top:42px;position:relative;z-index:1;">
      <div class="metric">Namespace<b>{html.escape(str(summary["target_namespace"]))}</b></div>
      <div class="metric">State<b>{html.escape(str(summary["state"]))}</b></div>
      <div class="metric">Steps<b>{summary["completed_steps"]}/{summary["total_steps"]}</b></div>
      <div class="metric">Failures<b>{summary["failed_steps"]}</b></div>
    </div>
    <div class="detail-grid">
      <div class="label">REPORT TYPE</div><div>{html.escape(str(context["report_type"]))}</div>
      <div class="label">SOURCE</div><div>{source_namespace}</div>
      <div class="label">TARGET</div><div>{html.escape(str(summary["target_namespace"]))}</div>
      <div class="label">CORRELATION</div><div>{correlation_id}</div>
      <div class="label">GENERATED</div><div>{html.escape(str(context["generated_at"]))}</div>
    </div>
  </section>
  <section class="page">
    <div class="page-header">
      <strong>BOS Genesis Execution Evidence</strong><span>Executive Summary</span>
    </div>
    <h2>Executive Summary</h2>
    <p class="intro">
      This document summarizes the controlled namespace execution, approval checkpoint,
      mutation evidence, validation results, and audit trail in an operator-readable format.
    </p>
    <section class="grid">
      <div class="metric">Mutations<b>{summary["mutation_step_count"]}</b></div>
      <div class="metric">Validations<b>{summary["validation_step_count"]}</b></div>
      <div class="metric">Observations<b>{summary["observation_count"]}</b></div>
      <div class="metric">Audit Events<b>{summary["audit_event_count"]}</b></div>
    </section>
    <h3>Warnings And Control Gates</h3>
    <p>{warning_chips}</p>
    {gate_table}
    <h3>Mutation Results</h3>
    {_html_table(mutation_rows, ["phase", "step", "action", "severity", "summary"])}
  </section>
  <section class="page">
    <div class="page-header">
      <strong>BOS Genesis Execution Evidence</strong><span>Validation And Resources</span>
    </div>
    <h2>Validation Results</h2>
    {validation_table}
    <h2>Resource Change Table</h2>
    {_html_table(resource_rows, ["step", "phase", "type", "state"])}
    <h2>Observation Trace</h2>
    {_html_table(_observation_rows(context), ["type", "severity", "summary"])}
  </section>
</body>
</html>
"""


def _write_pdf(path: Path, context: dict[str, Any]) -> None:
    try:
        _write_reportlab_pdf(path, context)
    except ModuleNotFoundError:
        _write_builtin_pdf(path, context)


def _write_reportlab_pdf(path: Path, context: dict[str, Any]) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    summary = context["summary"]
    styles = getSampleStyleSheet()
    theme = _pdf_theme()
    title_style = ParagraphStyle(
        "BosGenesisTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=27,
        textColor=colors.HexColor(theme["primary"]),
        spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "BosGenesisSection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor(theme["primary"]),
        spaceBefore=18,
        spaceAfter=8,
    )
    body_style = ParagraphStyle(
        "BosGenesisBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.2,
        leading=12.5,
        textColor=colors.HexColor(theme["text"]),
    )
    intro_style = ParagraphStyle(
        "BosGenesisIntro",
        parent=body_style,
        fontSize=10.4,
        leading=14,
        textColor=colors.HexColor("#28456d"),
        borderColor=colors.HexColor(theme["accent"]),
        borderWidth=0,
        borderPadding=0,
        spaceAfter=8,
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        topMargin=0.95 * inch,
        bottomMargin=0.5 * inch,
        title=str(context["title"]),
        author="BOS Genesis MoP Execution Agent",
    )
    story: list[Any] = [
        Spacer(1, 620),
        PageBreak(),
        Paragraph("Executive Summary", title_style),
        Paragraph(
            "This report summarizes deterministic execution evidence, approval gates, "
            "namespace mutations, validation checks, and audit context for operator review.",
            intro_style,
        ),
        Spacer(1, 8),
        _pdf_metric_grid(
            [
                ("State", summary["state"], theme["accent"]),
                (
                    "Steps",
                    f"{summary['completed_steps']}/{summary['total_steps']}",
                    theme["success"],
                ),
                ("Failures", str(summary["failed_steps"]), theme["danger"]),
                ("Audit Events", str(summary["audit_event_count"]), theme["secondary"]),
            ]
        ),
        Spacer(1, 10),
    ]
    story.append(
        _pdf_table(
            [
                ["Target namespace", str(summary["target_namespace"])],
                ["Job state", str(summary["state"])],
                [
                    "Source namespace",
                    str((context.get("sections") or {}).get("source_namespace") or ""),
                ],
                ["Correlation ID", str(summary.get("correlation_id") or "")],
                ["Trace ID", str(summary.get("trace_id") or "")],
                ["Steps", f"{summary['completed_steps']}/{summary['total_steps']}"],
                ["Failed steps", str(summary["failed_steps"])],
                ["Observations", str(summary["observation_count"])],
                ["Audit events", str(summary["audit_event_count"])],
            ],
            col_widths=[1.7 * inch, 4.2 * inch],
        )
    )
    story.extend([Spacer(1, 8), Paragraph("Warnings And Control Gates", section_style)])
    for warning in context.get("warnings") or ["None"]:
        story.append(Paragraph(f"- {_xml(str(warning))}", body_style))
    gate_rows = _policy_gate_rows(context)
    if gate_rows:
        story.append(
            _pdf_table(
                [["Severity", "Step", "Codes", "Summary"]]
                + [
                    [
                        row["severity"],
                        row["step_id"],
                        row["codes"],
                        row["summary"],
                    ]
                    for row in gate_rows[:8]
                ],
                header=True,
                col_widths=[0.75 * inch, 1.6 * inch, 1.25 * inch, 2.9 * inch],
            )
        )
    else:
        story.append(Paragraph("No blocking policy gates remained at completion.", body_style))
    story.extend([Paragraph("Mutation Results", section_style)])
    story.append(
        _pdf_table(
            [["Phase", "Step", "Action", "Severity", "Summary"]]
            + [
                [
                    row["phase"],
                    row["step"],
                    row["action"],
                    row["severity"],
                    row["summary"],
                ]
                for row in _mutation_rows(context)[:16]
            ],
            header=True,
            col_widths=[1.15 * inch, 1.75 * inch, 1.05 * inch, 0.75 * inch, 2.05 * inch],
        )
    )
    validation_rows = _validation_rows(context)
    if validation_rows:
        story.extend([Paragraph("Validation Results", section_style)])
        story.append(
            _pdf_table(
                [["Check", "Status", "Summary"]]
                + [[row["check"], row["status"], row["summary"]] for row in validation_rows[:18]],
                header=True,
                col_widths=[1.75 * inch, 0.8 * inch, 4.35 * inch],
            )
        )
    story.extend([Paragraph("Resource Change Table", section_style)])
    story.append(
        _pdf_table(
            [["Step", "Phase", "Type", "State"]]
            + [
                [
                    row["step"],
                    row["phase"],
                    row["type"],
                    row["state"],
                ]
                for row in _resource_change_rows(context)[:35]
            ],
            header=True,
            col_widths=[2.35 * inch, 1.45 * inch, 1.15 * inch, 1.25 * inch],
        )
    )
    story.extend([Paragraph("Observation Trace", section_style)])
    story.append(
        _pdf_table(
            [["Type", "Severity", "Summary"]]
            + [
                [
                    row["type"],
                    row["severity"],
                    row["summary"],
                ]
                for row in _observation_rows(context)[-18:]
            ],
            header=True,
            col_widths=[1.35 * inch, 0.85 * inch, 4.7 * inch],
        )
    )
    appendix_rows = _appendix_rows(context)
    if appendix_rows:
        story.extend([Paragraph("Evidence Appendix", section_style)])
        story.append(
            _pdf_table(
                [["Section", "Summary"]]
                + [[row["section"], row["summary"]] for row in appendix_rows],
                header=True,
                col_widths=[1.8 * inch, 5.1 * inch],
            )
        )
    doc.build(
        story,
        onFirstPage=lambda canvas, document: _draw_cover_page(canvas, document, context),
        onLaterPages=lambda canvas, document: _draw_page_chrome(canvas, document, context),
    )


def _pdf_table(
    rows: list[list[str]],
    *,
    header: bool = False,
    col_widths: list[float] | None = None,
) -> Any:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    theme = _pdf_theme()
    body = ParagraphStyle(
        "BosGenesisTableCell",
        fontName="Helvetica",
        fontSize=7.2,
        leading=9.2,
        textColor=colors.HexColor(theme["text"]),
    )
    head = ParagraphStyle(
        "BosGenesisTableHead",
        parent=body,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor(theme["primary"]),
    )
    if not rows:
        rows = [["No records"]]
    rendered = [
        [
            Paragraph(_xml(str(cell)), head if header and row_index == 0 else body)
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(
        rendered,
        colWidths=col_widths or [6.9 * inch / max(len(rendered[0]), 1)] * len(rendered[0]),
        hAlign="LEFT",
        repeatRows=1 if header else 0,
        splitByRow=True,
    )
    style = [
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor(theme["border"])),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        (
            "ROWBACKGROUNDS",
            (0, 1 if header else 0),
            (-1, -1),
            [colors.white, colors.HexColor("#f7fbfd")],
        ),
    ]
    if header:
        style.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf7fa")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(theme["primary"])),
            ]
        )
    table.setStyle(TableStyle(style))
    return table


def _pdf_metric_grid(cards: list[tuple[str, str, str]]) -> Any:
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    rows = []
    for label, value, color in cards:
        rows.append(
            Paragraph(
                f"<font color='{color}' size='15'><b>{_xml(value)}</b></font><br/>"
                f"<font color='#63737f' size='7'>{_xml(label.upper())}</font>",
                _metric_style(),
            )
        )
    table = Table([rows], colWidths=[1.62 * inch] * 4, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#c2d6e0")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c2d6e0")),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def _pdf_theme() -> dict[str, str]:
    return {
        "primary": "#0d2e52",
        "secondary": "#006e9e",
        "accent": "#00a1b8",
        "success": "#007a54",
        "warning": "#f38c1f",
        "danger": "#b81f2e",
        "panel": "#fbfdff",
        "border": "#c2d6e0",
        "text": "#1a242e",
        "muted": "#63737f",
        "brand": "#e20074",
    }


def _metric_style() -> Any:
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle(
        "BosGenesisMetric",
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        spaceAfter=0,
    )


def _draw_cover_page(canvas: Any, document: Any, context: dict[str, Any]) -> None:
    from reportlab.lib import colors

    theme = _pdf_theme()
    width, height = document.pagesize
    summary = context["summary"]
    sections = context.get("sections") or {}
    canvas.saveState()
    canvas.setFillColor(colors.HexColor(theme["primary"]))
    canvas.rect(0, 0, width, height, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor("#082743"))
    canvas.circle(width - 78, height - 78, 82, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(theme["accent"]))
    canvas.circle(width - 135, height - 118, 40, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(theme["secondary"]))
    canvas.rect(0, 0, width, 138, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(theme["accent"]))
    canvas.rect(0, 138, width, 10, fill=1, stroke=0)

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 15)
    canvas.drawString(56, height - 92, "BOS GENESIS")
    canvas.setFont("Helvetica-Bold", 31)
    title_y = height - 142
    title_lines = _cover_title_lines(str(context["title"]))
    for line_index, line in enumerate(title_lines):
        canvas.drawString(56, title_y - line_index * 37, line)
    subtitle_y = title_y - len(title_lines) * 37 - 4
    canvas.setFont("Helvetica", 12)
    canvas.setFillColor(colors.HexColor("#dff8fb"))
    canvas.drawString(58, subtitle_y, "MoP Execution Agent evidence package")
    canvas.drawString(
        58,
        subtitle_y - 23,
        _fit_canvas_text(
            f"{sections.get('source_namespace') or ''} -> {summary['target_namespace']}",
            75,
        ),
    )

    cards = [
        ("STATE", str(summary["state"]), theme["accent"]),
        ("STEPS", f"{summary['completed_steps']}/{summary['total_steps']}", theme["success"]),
        ("FAILED", str(summary["failed_steps"]), theme["danger"]),
        ("AUDIT EVENTS", str(summary["audit_event_count"]), theme["secondary"]),
    ]
    card_y = subtitle_y - 145
    x = 58
    for label, value, color in cards:
        _draw_cover_metric(canvas, x, card_y, 112, 70, label, value, color)
        x += 122

    y = card_y - 85
    details = [
        ("REPORT TYPE", str(context["report_type"])),
        ("JOB ID", str((context.get("job") or {}).get("job_id") or "")),
        ("CORRELATION ID", str(summary.get("correlation_id") or "")),
        ("TRACE ID", str(summary.get("trace_id") or "")),
        ("GENERATED", str(context["generated_at"])),
    ]
    for label, value in details:
        canvas.setFillColor(colors.HexColor("#85f2ff"))
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.drawString(58, y, label)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(165, y, _fit_canvas_text(value, 78))
        y -= 22

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(58, 86, "Operator-ready evidence. Human review remains required.")
    canvas.setFont("Helvetica", 8.7)
    canvas.setFillColor(colors.HexColor("#e7f8fb"))
    canvas.drawString(
        58,
        66,
        "Secret values, tokens, and sensitive payloads are redacted before report rendering.",
    )
    canvas.restoreState()


def _draw_cover_metric(
    canvas: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    label: str,
    value: str,
    color: str,
) -> None:
    from reportlab.lib import colors

    canvas.setFillColor(colors.white)
    canvas.roundRect(x, y, width, height, 4, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(color))
    canvas.rect(x, y + height - 8, width, 8, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(color))
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(x + 10, y + height - 30, _fit_canvas_text(value, 13))
    canvas.setFillColor(colors.HexColor("#63737f"))
    canvas.setFont("Helvetica", 7.2)
    canvas.drawString(x + 10, y + 14, label)


def _draw_page_chrome(canvas: Any, document: Any, context: dict[str, Any]) -> None:
    from reportlab.lib import colors

    theme = _pdf_theme()
    width, height = document.pagesize
    canvas.saveState()
    canvas.setFillColor(colors.HexColor(theme["primary"]))
    canvas.rect(0, height - 48, width, 48, fill=1, stroke=0)
    canvas.setFillColor(colors.HexColor(theme["accent"]))
    canvas.rect(0, height - 53, width, 5, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawString(document.leftMargin, height - 29, "BOS Genesis MoP Execution Agent")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(width - document.rightMargin, height - 29, str(context["report_type"]))
    canvas.setFillColor(colors.HexColor(theme["border"]))
    canvas.line(document.leftMargin, 34, width - document.rightMargin, 34)
    canvas.setFillColor(colors.HexColor(theme["muted"]))
    canvas.setFont("Helvetica", 7.2)
    canvas.drawString(document.leftMargin, 20, "Redacted operator evidence")
    canvas.drawRightString(width - document.rightMargin, 20, f"Page {document.page}")
    canvas.restoreState()


def _mutation_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in context.get("observations") or []:
        if item.get("observation_type") != "mutation_result":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        rows.append(
            {
                "phase": _short(item.get("phase_id") or ""),
                "step": _short(item.get("step_id") or ""),
                "action": _short(result.get("action") or "mutation"),
                "severity": _short(item.get("severity") or ""),
                "summary": _short(item.get("summary") or "", 150),
            }
        )
    if rows:
        return rows
    return [
        {
            "phase": row["phase"],
            "step": row["step"],
            "action": row["type"],
            "severity": "info",
            "summary": f"Step ended in {row['state']}.",
        }
        for row in _resource_change_rows(context)
        if row["type"] in {"k8s_apply", "k8s_delete", "helm_install", "helm_upgrade"}
    ]


def _validation_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    sections = context.get("sections") or {}
    candidates: list[dict[str, Any]] = []
    validation = sections.get("validation_result")
    if isinstance(validation, dict):
        candidates.extend(
            check for check in validation.get("checks", []) if isinstance(check, dict)
        )
    for item in sections.get("validation_observations") or []:
        result = item.get("result") if isinstance(item, dict) else None
        if isinstance(result, dict):
            candidates.extend(
                check for check in result.get("checks", []) if isinstance(check, dict)
            )
            nested = result.get("validation")
            if isinstance(nested, dict):
                candidates.extend(
                    check for check in nested.get("checks", []) if isinstance(check, dict)
                )
    rows = []
    seen: set[tuple[str, str]] = set()
    for check in candidates:
        name = str(check.get("name") or "validation")
        summary = _short(check.get("summary") or "", 165)
        key = (name, summary)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "check": _short(name, 70),
                "status": "passed" if check.get("success") is not False else "failed",
                "summary": summary,
            }
        )
    return rows


def _resource_change_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    sections = context.get("sections") or {}
    source = sections.get("resource_change_table")
    rows: list[dict[str, str]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "step": _short(item.get("step_id") or ""),
                    "phase": _short(item.get("phase_id") or ""),
                    "type": _short(item.get("type") or ""),
                    "state": _short(item.get("state") or ""),
                }
            )
    if rows:
        return rows
    for step in context.get("steps") or []:
        rows.append(
            {
                "step": _short(step.get("step_id") or ""),
                "phase": _short(step.get("phase_id") or ""),
                "type": _short(step.get("type") or ""),
                "state": _short(step.get("state") or ""),
            }
        )
    return rows


def _policy_gate_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in context.get("observations") or []:
        blocks = item.get("policy_blocks") or []
        if not blocks:
            continue
        codes = ",".join(
            str(block.get("code") if isinstance(block, dict) else block) for block in blocks
        )
        rows.append(
            {
                "severity": _short(item.get("severity") or ""),
                "step_id": _short(item.get("step_id") or ""),
                "codes": _short(codes, 80),
                "summary": _short(item.get("summary") or "", 155),
            }
        )
    return rows


def _observation_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "type": _short(item.get("observation_type") or "", 60),
            "severity": _short(item.get("severity") or "", 40),
            "summary": _short(item.get("summary") or "", 180),
        }
        for item in (context.get("observations") or [])
    ]


def _appendix_rows(context: dict[str, Any]) -> list[dict[str, str]]:
    skipped = {
        "target_namespace",
        "source_namespace",
        "resource_change_table",
        "mutation_observations",
        "validation_observations",
    }
    rows: list[dict[str, str]] = []
    for name, value in (context.get("sections") or {}).items():
        if name in skipped:
            continue
        rows.append({"section": _title(str(name)), "summary": _section_brief(value)})
    return rows


def _section_brief(value: Any) -> str:
    if isinstance(value, str):
        return _short(value, 190)
    if isinstance(value, dict):
        if "success" in value and "checks" in value:
            checks = value.get("checks") if isinstance(value.get("checks"), list) else []
            return f"success={value.get('success')}; checks={len(checks)}"
        if "rollback" in value and isinstance(value["rollback"], dict):
            steps = value["rollback"].get("steps") or []
            return f"rollback summary; steps={len(steps)}"
        return _short(
            ", ".join(f"{key}={_brief_value(item)}" for key, item in list(value.items())[:8]),
            190,
        )
    if isinstance(value, list):
        return f"{len(value)} records"
    return _short(str(value), 190)


def _brief_value(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return f"{len(value)} fields"
    return type(value).__name__


def _short(value: Any, limit: int = 95) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _fit_canvas_text(value: str, limit: int) -> str:
    return _short(value, limit)


def _cover_title_lines(value: str) -> list[str]:
    return textwrap.wrap(value, width=34, break_long_words=False)[:3] or [value]


def _xml(value: str) -> str:
    return html.escape(str(value), quote=False)


def _write_builtin_pdf(path: Path, context: dict[str, Any]) -> None:
    text = _markdown(context)
    lines = _pdf_wrapped_lines(text)
    page_lines = [lines[index : index + 54] for index in range(0, len(lines), 54)] or [[]]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{4 + index * 2} 0 R" for index in range(len(page_lines)))
            + f"] /Count {len(page_lines)} >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, lines_for_page in enumerate(page_lines):
        page_object_id = 4 + index * 2
        content_object_id = page_object_id + 1
        stream = _pdf_text_stream(lines_for_page)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object_id} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    path.write_bytes(_pdf_payload(objects))


def _pdf_wrapped_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        normalized = raw_line.encode("latin-1", errors="replace").decode("latin-1")
        if not normalized:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(normalized, width=94, replace_whitespace=False) or [""])
    return lines


def _pdf_text_stream(lines: list[str]) -> bytes:
    commands = ["BT", "/F1 9 Tf", "42 756 Td", "12 TL"]
    for line in lines:
        commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("T*")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1", errors="replace")


def _pdf_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _pdf_payload(objects: list[bytes]) -> bytes:
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{object_number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _step_table(steps: list[dict[str, Any]]) -> str:
    lines = ["| Step | Phase | Type | State |", "| --- | --- | --- | --- |"]
    for step in steps:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{step.get('step_id', '')}`",
                    f"`{step.get('phase_id', '')}`",
                    f"`{step.get('type', '')}`",
                    f"`{step.get('state', '')}`",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _observation_table(observations: list[dict[str, Any]]) -> str:
    lines = ["| Type | Severity | Summary |", "| --- | --- | --- |"]
    for item in observations[-25:]:
        lines.append(
            f"| `{item.get('observation_type', '')}` | `{item.get('severity', '')}` | "
            f"{str(item.get('summary', '')).replace('|', '/')} |"
        )
    return "\n".join(lines)


def _html_table(rows: list[dict[str, Any]], fields: list[str]) -> str:
    header = "".join(f"<th>{html.escape(field)}</th>" for field in fields)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(
            f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in fields
        ) + "</tr>"
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _html_sections(sections: dict[str, Any]) -> str:
    return "".join(
        f"<h3>{html.escape(_title(str(name)))}</h3><pre>{html.escape(_section_value(value))}</pre>"
        for name, value in sections.items()
    )


def _section_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(redact_value(value))


def _title(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()
