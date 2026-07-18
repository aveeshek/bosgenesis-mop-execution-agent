"""Explainable, rules-first runtime risk assessment for Namespace Twins."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.security import redact_value

RUNTIME_BEHAVIOR_RULES_VERSION = "namespace-twin-runtime-behavior-1.0.0"
DEFAULT_EVENT_WINDOW_MINUTES = 60

CRASH_REASONS = {"crashloopbackoff", "backoff", "oomkilled"}
IMAGE_REASONS = {"imagepullbackoff", "errimagepull", "failedpullimage"}
PRESSURE_TOKENS = {
    "memorypressure",
    "diskpressure",
    "pidpressure",
    "outofmemory",
    "oomkilled",
    "evicted",
    "insufficient cpu",
    "insufficient memory",
    "failedscheduling",
}


def assess_runtime_behavior(
    snapshot: LiveSnapshot,
    *,
    namespace_summary: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    captured_at: datetime | None = None,
    target_namespace: str,
    event_window_minutes: int = DEFAULT_EVENT_WINDOW_MINUTES,
) -> dict[str, Any]:
    """Score current runtime facts without granting approval authority."""
    now = (captured_at or datetime.now(UTC)).astimezone(UTC)
    pods = [_pod_summary(item) for item in snapshot.resources if _kind(item) == "Pod"]
    pods = [item for item in pods if item]
    recent_events = _recent_events(events or [], now, event_window_minutes)

    not_ready = [item for item in pods if not item["ready"]]
    restarting = [item for item in pods if item["restarts"] > 0]
    high_restart = [item for item in pods if item["restarts"] >= 5]
    crash_events = [item for item in recent_events if _reason(item) in CRASH_REASONS]
    image_events = [item for item in recent_events if _reason(item) in IMAGE_REASONS]
    warning_events = [
        item for item in recent_events if str(item.get("type") or "").lower() == "warning"
    ]
    pressure_events = [item for item in recent_events if _is_pressure_event(item)]
    pressure = _resource_pressure(pressure_events)

    evidence_refs = _evidence_refs(
        snapshot=snapshot,
        captured_at=now,
        target_namespace=target_namespace,
        has_events=bool(events is not None),
    )
    factors: list[dict[str, Any]] = []
    score = 0
    if not snapshot.available:
        factors.append(
            _factor(
                "runtime_snapshot_unavailable",
                "Runtime snapshot unavailable",
                "unknown",
                0.35,
                snapshot.warning or "The current namespace snapshot could not be collected.",
                evidence_refs,
            )
        )
    if not_ready:
        contribution = min(30, 10 + (len(not_ready) - 1) * 5)
        score += contribution
        factors.append(
            _factor(
                "runtime_pods_not_ready",
                "Pods are not ready",
                "increases_risk",
                0.95,
                f"{len(not_ready)} pod(s) are not ready: {_pod_names(not_ready)}.",
                evidence_refs,
            )
        )
    if restarting:
        contribution = 20 if high_restart else 10
        score += contribution
        factors.append(
            _factor(
                "runtime_pods_restarting",
                "Pod restarts observed",
                "increases_risk",
                0.92,
                f"{len(restarting)} pod(s) have restarts; "
                f"{len(high_restart)} crossed the five-restart threshold.",
                evidence_refs,
            )
        )
    if crash_events:
        score += 30
        factors.append(
            _factor(
                "runtime_crashloop_events",
                "Crash-loop signals detected",
                "increases_risk",
                0.96,
                f"{len(crash_events)} recent crash/backoff event(s) were observed.",
                evidence_refs,
            )
        )
    if image_events:
        score += 25
        factors.append(
            _factor(
                "runtime_image_pull_events",
                "Image pull failures detected",
                "increases_risk",
                0.96,
                f"{len(image_events)} recent image-pull failure event(s) were observed.",
                evidence_refs,
            )
        )
    if warning_events:
        score += min(15, 3 + len(warning_events))
        factors.append(
            _factor(
                "runtime_warning_events",
                "Kubernetes warning events",
                "increases_risk",
                0.88,
                f"{len(warning_events)} recent Warning event(s) fall inside the "
                f"{event_window_minutes}-minute window.",
                evidence_refs,
            )
        )
    if pressure != "none":
        score += {"low": 8, "medium": 18, "high": 30, "unknown": 0}[pressure]
        factors.append(
            _factor(
                "runtime_resource_pressure",
                "Resource pressure signals",
                "unknown" if pressure == "unknown" else "increases_risk",
                0.9 if pressure != "unknown" else 0.45,
                f"Runtime event evidence indicates {pressure} resource pressure.",
                evidence_refs,
            )
        )
    if snapshot.available and not factors:
        factors.append(
            _factor(
                "runtime_no_active_anomaly",
                "No active runtime anomaly detected",
                "reduces_risk",
                0.9,
                "Collected pod and event facts contain no deterministic runtime-risk signal.",
                evidence_refs,
            )
        )

    score = min(score, 100)
    risk = _risk(score, available=snapshot.available)
    health = _health(
        available=snapshot.available,
        not_ready=len(not_ready),
        restarting=len(restarting),
        crash_events=len(crash_events),
        image_events=len(image_events),
        pressure=pressure,
    )
    confidence = _confidence(
        snapshot_available=snapshot.available,
        pods_collected="Pod" in snapshot.complete_kinds,
        events_collected=events is not None,
        warning=bool(snapshot.warning),
    )
    summary = _summary(health, risk, len(not_ready), len(restarting), len(warning_events))
    payload = {
        "method": "rules_only",
        "risk": risk,
        "risk_score": score,
        "confidence": confidence,
        "current_health": {
            "status": health,
            "not_ready_pods": len(not_ready),
            "restarting_pods": len(restarting),
            "event_anomalies": len(warning_events),
            "resource_pressure": pressure,
        },
        "pod_details": pods,
        "recent_event_counts": {
            "total": len(recent_events),
            "warnings": len(warning_events),
            "crash_loop": len(crash_events),
            "image_pull": len(image_events),
            "resource_pressure": len(pressure_events),
        },
        "event_window_minutes": event_window_minutes,
        "factors": factors,
        "historical_context_status": "not_available",
        "historical_context_message": (
            "Not Available: validated historical runtime-comparison APIs are not configured."
        ),
        "may_independently_approve": False,
        "execution_effect": _execution_effect(risk),
        "rules_version": RUNTIME_BEHAVIOR_RULES_VERSION,
        "captured_at": now.isoformat(),
        "target_namespace": target_namespace,
        "namespace_summary": redact_value(namespace_summary or {}),
        "evidence_refs": evidence_refs,
        "summary": summary,
        "model_authority": False,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    payload["assessment_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _pod_summary(resource: dict[str, Any]) -> dict[str, Any]:
    summary = resource.get("summary") if isinstance(resource.get("summary"), dict) else resource
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    name = str(summary.get("name") or metadata.get("name") or "unknown")
    phase = str(summary.get("phase") or (resource.get("status") or {}).get("phase") or "Unknown")
    ready_value = summary.get("ready")
    ready = _ready(ready_value, phase)
    restarts = _integer(summary.get("restarts"))
    if not restarts:
        statuses = (resource.get("status") or {}).get("containerStatuses") or []
        restarts = sum(
            _integer(item.get("restartCount")) for item in statuses if isinstance(item, dict)
        )
    return {
        "name": name,
        "phase": phase,
        "ready": ready,
        "ready_value": str(ready_value or "unknown"),
        "restarts": restarts,
    }


def _kind(resource: dict[str, Any]) -> str:
    return str(resource.get("kind") or "")


def _ready(value: Any, phase: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and "/" in value:
        current, total = value.split("/", 1)
        try:
            return int(total) > 0 and int(current) == int(total) and phase == "Running"
        except ValueError:
            pass
    return phase in {"Succeeded", "Completed"}


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _recent_events(
    events: list[dict[str, Any]], now: datetime, event_window_minutes: int
) -> list[dict[str, Any]]:
    threshold = now - timedelta(minutes=max(1, event_window_minutes))
    rows = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = _timestamp(event.get("last_timestamp") or event.get("event_time"))
        if timestamp is None or timestamp >= threshold:
            rows.append(redact_value(event))
    return rows


def _timestamp(value: Any) -> datetime | None:
    if not value or str(value) == "None":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _reason(event: dict[str, Any]) -> str:
    return str(event.get("reason") or "").replace(" ", "").lower()


def _is_pressure_event(event: dict[str, Any]) -> bool:
    text = " ".join(str(event.get(key) or "") for key in ("reason", "message", "object")).lower()
    return any(token in text for token in PRESSURE_TOKENS)


def _resource_pressure(events: list[dict[str, Any]]) -> str:
    if not events:
        return "none"
    text = json.dumps(events, default=str).lower()
    if any(
        token in text for token in ("outofmemory", "oomkilled", "diskpressure", "memorypressure")
    ):
        return "high"
    return "medium" if len(events) >= 2 else "low"


def _risk(score: int, *, available: bool) -> str:
    if not available:
        return "unknown"
    if score >= 75:
        return "critical"
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _health(
    *,
    available: bool,
    not_ready: int,
    restarting: int,
    crash_events: int,
    image_events: int,
    pressure: str,
) -> str:
    if not available:
        return "unknown"
    if crash_events or image_events or pressure == "high" or not_ready >= 3:
        return "unhealthy"
    if not_ready or restarting or pressure in {"low", "medium"}:
        return "degraded"
    return "healthy"


def _confidence(
    *, snapshot_available: bool, pods_collected: bool, events_collected: bool, warning: bool
) -> float:
    score = 0.25
    score += 0.3 if snapshot_available else 0
    score += 0.25 if pods_collected else 0
    score += 0.2 if events_collected else 0
    score -= 0.1 if warning else 0
    return round(max(0.1, min(score, 1.0)), 2)


def _execution_effect(risk: str) -> str:
    return {
        "critical": "force_red",
        "high": "force_amber",
        "medium": "force_amber",
        "low": "no_uplift",
        "unknown": "no_uplift",
    }[risk]


def _summary(health: str, risk: str, not_ready: int, restarting: int, warnings: int) -> str:
    return (
        f"Current namespace health is {health}; deterministic runtime risk is {risk}. "
        f"Observed {not_ready} not-ready pod(s), {restarting} restarting pod(s), and "
        f"{warnings} recent Warning event(s)."
    )


def _pod_names(pods: list[dict[str, Any]]) -> str:
    names = [str(item.get("name") or "unknown") for item in pods]
    rendered = ", ".join(names[:8])
    return rendered + (f" and {len(names) - 8} more" if len(names) > 8 else "")


def _factor(
    factor_id: str,
    title: str,
    impact: str,
    confidence: float,
    summary: str,
    evidence_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "factor_id": factor_id,
        "title": title,
        "impact": impact,
        "confidence": confidence,
        "summary": summary,
        "evidence_refs": evidence_refs,
    }


def _evidence_refs(
    *,
    snapshot: LiveSnapshot,
    captured_at: datetime,
    target_namespace: str,
    has_events: bool,
) -> list[dict[str, Any]]:
    refs = [
        {
            "evidence_id": "runtime_snapshot_"
            + hashlib.sha256(f"{target_namespace}:{captured_at.isoformat()}".encode()).hexdigest()[
                :24
            ],
            "source_type": "live_snapshot",
            "source_id": target_namespace,
            "summary": "Namespace-scoped pod and workload health snapshot.",
            "captured_at": captured_at.isoformat(),
            "redacted": True,
            "href": None,
        }
    ]
    if has_events:
        refs.append(
            {
                "evidence_id": "runtime_events_"
                + hashlib.sha256(
                    f"events:{target_namespace}:{captured_at.isoformat()}".encode()
                ).hexdigest()[:24],
                "source_type": "kubernetes",
                "source_id": target_namespace,
                "summary": "Namespace-scoped recent Kubernetes event evidence.",
                "captured_at": captured_at.isoformat(),
                "redacted": True,
                "href": None,
            }
        )
    return refs
