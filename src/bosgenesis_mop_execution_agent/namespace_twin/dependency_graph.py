"""Deterministic Kubernetes dependency graph construction for Namespace Twins."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle

WORKLOAD_KINDS = {"CronJob", "DaemonSet", "Deployment", "Job", "Pod", "StatefulSet"}
CLUSTER_SCOPED_KINDS = {"ClusterRole", "CustomResourceDefinition", "Namespace"}
EDGE_TYPES = {
    "configmap_ref",
    "crd_owns_custom_resource",
    "helm_owns_resource",
    "owner_reference",
    "plan_applies",
    "plan_depends_on",
    "pvc_ref",
    "rbac_role_ref",
    "rbac_subject",
    "route_backend",
    "secret_name_ref",
    "selector_matches",
    "service_account_ref",
}


def stable_node_id(identity: str) -> str:
    return f"node_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def stable_edge_id(source: str, target: str, edge_type: str) -> str:
    material = f"{source}\0{edge_type}\0{target}"
    return f"edge_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def build_dependency_graph(
    bundle: ArtifactBundle,
    resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return persisted nodes, edges, graph findings, and deterministic summary counts."""
    nodes = [deepcopy(item) for item in resources]
    by_identity: dict[str, dict[str, Any]] = {}
    by_key: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    path_to_identities: dict[str, list[str]] = {}
    for node in nodes:
        payload = node.setdefault("payload_redacted", {})
        payload.setdefault("source", "rendered_manifest")
        payload.setdefault("status", "present")
        payload.setdefault("risk", _node_risk(str(node.get("kind") or ""), "present"))
        payload.setdefault("evidence_refs", [str(payload.get("path") or "bundle")])
        payload["node_id"] = stable_node_id(node["stable_identity"])
        by_identity[node["stable_identity"]] = node
        by_key.setdefault((str(node["kind"]), node.get("namespace"), str(node["name"])), []).append(
            node
        )
        path = str(payload.get("path") or "").replace("\\", "/")
        if path:
            path_to_identities.setdefault(path, []).append(node["stable_identity"])

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_synthetic(
        kind: str,
        name: str,
        namespace: str | None,
        *,
        status: str,
        source: str,
        evidence_refs: Iterable[str],
        details: dict[str, Any] | None = None,
    ) -> str:
        api_version = "reference.esda/v1"
        identity = f"{api_version}:{kind}:{namespace or '_cluster'}:{name}"
        if identity in by_identity:
            return identity
        evidence = sorted({str(item) for item in evidence_refs if str(item).strip()}) or ["bundle"]
        node = {
            "resource_id": f"twinres_{uuid4().hex}",
            "stable_identity": identity,
            "api_version": api_version,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "payload_redacted": {
                "node_id": stable_node_id(identity),
                "source": source,
                "status": status,
                "risk": _node_risk(kind, status),
                "synthetic": True,
                "evidence_refs": evidence,
                "details": details or {},
            },
        }
        nodes.append(node)
        by_identity[identity] = node
        by_key.setdefault((kind, namespace, name), []).append(node)
        return identity

    def resolve(
        kind: str,
        name: str,
        namespace: str | None,
        *,
        evidence_refs: Iterable[str],
        missing_source: str = "unresolved_reference",
    ) -> tuple[str, str]:
        candidates = by_key.get((kind, namespace, name), [])
        if not candidates and kind in CLUSTER_SCOPED_KINDS:
            candidates = by_key.get((kind, None, name), [])
        if len(candidates) == 1:
            return candidates[0]["stable_identity"], "deterministic"
        if len(candidates) > 1:
            identity = add_synthetic(
                kind,
                name,
                namespace,
                status="uncertain",
                source="ambiguous_reference",
                evidence_refs=evidence_refs,
                details={
                    "candidate_identities": sorted(item["stable_identity"] for item in candidates)
                },
            )
            return identity, "uncertain"
        return (
            add_synthetic(
                kind,
                name,
                namespace,
                status="missing",
                source=missing_source,
                evidence_refs=evidence_refs,
            ),
            "deterministic",
        )

    def add_edge(
        source: str,
        target: str,
        edge_type: str,
        *,
        confidence: str,
        evidence_refs: Iterable[str],
    ) -> None:
        if edge_type not in EDGE_TYPES or source == target:
            return
        signature = (source, target, edge_type)
        if signature in seen_edges:
            return
        evidence = sorted({str(item) for item in evidence_refs if str(item).strip()}) or ["bundle"]
        seen_edges.add(signature)
        edges.append(
            {
                "edge_id": f"twinedge_{uuid4().hex}",
                "source_identity": source,
                "target_identity": target,
                "edge_type": edge_type,
                "confidence": confidence,
                "evidence_refs": evidence,
            }
        )

    # Extract dependencies from the selected Twin projection, not only from
    # the bundle loader's original manifest list. Explicit Helm installs can
    # add safe rendered resources to this projection, and those resources must
    # participate in route/reference analysis as first-class planned facts.
    for node in list(nodes):
        payload = node.get("payload_redacted") or {}
        if payload.get("synthetic") is True:
            continue
        content = payload.get("manifest") or {}
        if not isinstance(content, dict):
            continue
        namespace = node.get("namespace")
        consumer = str(node["stable_identity"])
        path_refs = list(payload.get("evidence_refs") or [payload.get("path") or "bundle"])
        metadata = content.get("metadata") or {}

        for owner in metadata.get("ownerReferences") or []:
            if not isinstance(owner, dict) or not owner.get("kind") or not owner.get("name"):
                continue
            owner_id, confidence = resolve(
                str(owner["kind"]), str(owner["name"]), namespace, evidence_refs=path_refs
            )
            add_edge(
                owner_id,
                consumer,
                "owner_reference",
                confidence=confidence,
                evidence_refs=path_refs,
            )

        release = _helm_release(metadata)
        if release:
            helm_id = add_synthetic(
                "HelmRelease",
                release,
                namespace,
                status="present",
                source="helm_metadata",
                evidence_refs=path_refs,
            )
            add_edge(
                helm_id,
                consumer,
                "helm_owns_resource",
                confidence="deterministic",
                evidence_refs=path_refs,
            )

        for kind, name in _workload_references(str(node.get("kind") or ""), content):
            ref_id, confidence = resolve(kind, name, namespace, evidence_refs=path_refs)
            edge_type = {
                "ConfigMap": "configmap_ref",
                "Secret": "secret_name_ref",
                "PersistentVolumeClaim": "pvc_ref",
                "ServiceAccount": "service_account_ref",
            }[kind]
            add_edge(ref_id, consumer, edge_type, confidence=confidence, evidence_refs=path_refs)

        if node.get("kind") == "Ingress":
            for service_name in _ingress_backends(content):
                service_id, confidence = resolve(
                    "Service", service_name, namespace, evidence_refs=path_refs
                )
                add_edge(
                    consumer,
                    service_id,
                    "route_backend",
                    confidence=confidence,
                    evidence_refs=path_refs,
                )

        if node.get("kind") == "RoleBinding":
            role_ref = (
                content.get("roleRef")
                or content.get("spec", {}).get("roleRef")
                or {}
            )
            if isinstance(role_ref, dict) and role_ref.get("kind") and role_ref.get("name"):
                role_id, confidence = resolve(
                    str(role_ref["kind"]),
                    str(role_ref["name"]),
                    None if str(role_ref["kind"]) == "ClusterRole" else namespace,
                    evidence_refs=path_refs,
                )
                add_edge(
                    role_id,
                    consumer,
                    "rbac_role_ref",
                    confidence=confidence,
                    evidence_refs=path_refs,
                )
            for subject in (
                content.get("subjects")
                or content.get("spec", {}).get("subjects")
                or []
            ):
                if (
                    not isinstance(subject, dict)
                    or subject.get("kind") != "ServiceAccount"
                    or not subject.get("name")
                ):
                    continue
                subject_id, confidence = resolve(
                    "ServiceAccount",
                    str(subject["name"]),
                    str(subject.get("namespace") or namespace),
                    evidence_refs=path_refs,
                )
                add_edge(
                    consumer,
                    subject_id,
                    "rbac_subject",
                    confidence=confidence,
                    evidence_refs=path_refs,
                )

    _add_selector_edges(nodes, by_key, add_synthetic, add_edge, bundle.target_namespace)
    _add_crd_edges(nodes, resolve, add_edge)
    _add_plan_edges(bundle, path_to_identities, add_synthetic, add_edge)

    findings = _graph_findings(nodes, edges)
    statuses = [_node_status(item) for item in nodes]
    summary = {
        "nodes": len(nodes),
        "edges": len(edges),
        "present": statuses.count("present"),
        "missing": statuses.count("missing"),
        "uncertain": statuses.count("uncertain"),
        "cycles": _cycle_count(nodes, edges),
        "findings": len(findings),
    }
    return nodes, edges, findings, summary


def _add_selector_edges(
    nodes: list[dict[str, Any]],
    by_key: dict[tuple[str, str | None, str], list[dict[str, Any]]],
    add_synthetic: Any,
    add_edge: Any,
    target_namespace: str,
) -> None:
    workloads: list[tuple[dict[str, Any], dict[str, str]]] = []
    for node in list(nodes):
        if node["kind"] not in WORKLOAD_KINDS:
            continue
        manifest = (node.get("payload_redacted") or {}).get("manifest") or {}
        labels = _pod_template_labels(node["kind"], manifest)
        workloads.append((node, labels))
    for (kind, namespace, _name), services in list(by_key.items()):
        if kind != "Service":
            continue
        for service in services:
            manifest = (service.get("payload_redacted") or {}).get("manifest") or {}
            selector = (manifest.get("spec") or {}).get("selector") or {}
            if not isinstance(selector, dict) or not selector:
                continue
            matches = [
                item
                for item, labels in workloads
                if item.get("namespace") == (namespace or target_namespace)
                and all(str(labels.get(str(key))) == str(value) for key, value in selector.items())
            ]
            evidence = [str((service.get("payload_redacted") or {}).get("path") or "bundle")]
            if not matches:
                target = add_synthetic(
                    "SelectorTarget",
                    _selector_name(selector),
                    namespace,
                    status="missing",
                    source="unmatched_service_selector",
                    evidence_refs=evidence,
                    details={"selector": selector},
                )
                add_edge(
                    service["stable_identity"],
                    target,
                    "selector_matches",
                    confidence="deterministic",
                    evidence_refs=evidence,
                )
            else:
                for match in matches:
                    add_edge(
                        service["stable_identity"],
                        match["stable_identity"],
                        "selector_matches",
                        confidence="deterministic",
                        evidence_refs=evidence,
                    )


def _add_crd_edges(nodes: list[dict[str, Any]], resolve: Any, add_edge: Any) -> None:
    definitions: dict[tuple[str, str], str] = {}
    # Synthetic graph nodes use the private reference.esda API group. They are
    # graph bookkeeping, not Kubernetes custom resources, and must never cause
    # recursive synthetic CRD dependencies.
    for node in list(nodes):
        payload = node.get("payload_redacted") or {}
        if payload.get("synthetic") is True:
            continue
        if node["kind"] != "CustomResourceDefinition":
            continue
        manifest = (node.get("payload_redacted") or {}).get("manifest") or {}
        spec = manifest.get("spec") or {}
        names = spec.get("names") or {}
        if spec.get("group") and names.get("kind"):
            definitions[(str(spec["group"]), str(names["kind"]))] = node["stable_identity"]
    for node in list(nodes):
        api_version = str(node.get("api_version") or "")
        group = api_version.split("/", 1)[0] if "/" in api_version else ""
        if not group or group in {
            "admissionregistration.k8s.io",
            "apiextensions.k8s.io",
            "apps",
            "authentication.k8s.io",
            "authorization.k8s.io",
            "autoscaling",
            "batch",
            "certificates.k8s.io",
            "coordination.k8s.io",
            "discovery.k8s.io",
            "events.k8s.io",
            "flowcontrol.apiserver.k8s.io",
            "networking.k8s.io",
            "reference.esda",
            "node.k8s.io",
            "policy",
            "rbac.authorization.k8s.io",
            "scheduling.k8s.io",
            "storage.k8s.io",
        }:
            continue
        definition = definitions.get((group, str(node["kind"])))
        evidence = [str((node.get("payload_redacted") or {}).get("path") or "bundle")]
        if not definition:
            definition, confidence = resolve(
                "CustomResourceDefinition",
                f"{str(node['kind']).lower()}s.{group}",
                None,
                evidence_refs=evidence,
            )
        else:
            confidence = "deterministic"
        add_edge(
            definition,
            node["stable_identity"],
            "crd_owns_custom_resource",
            confidence=confidence,
            evidence_refs=evidence,
        )


def _add_plan_edges(
    bundle: ArtifactBundle,
    path_to_identities: dict[str, list[str]],
    add_synthetic: Any,
    add_edge: Any,
) -> None:
    phase_ids: dict[str, str] = {}
    for phase in bundle.machine_plan.phases:
        phase_ids[phase.phase_id] = add_synthetic(
            "PlanPhase",
            phase.phase_id,
            bundle.target_namespace,
            status="present",
            source="machine_execution_plan",
            evidence_refs=["machine_execution_plan.yaml"],
            details={"title": phase.title, "objective": phase.objective},
        )
    dependencies = {
        entry.phase_id: list(entry.depends_on) for entry in bundle.machine_plan.dependency_graph
    }
    for phase in bundle.machine_plan.phases:
        current = phase_ids[phase.phase_id]
        for dependency in sorted(set(phase.depends_on) | set(dependencies.get(phase.phase_id, []))):
            dependency_id = phase_ids.get(dependency) or add_synthetic(
                "PlanPhase",
                dependency,
                bundle.target_namespace,
                status="missing",
                source="unresolved_plan_dependency",
                evidence_refs=["machine_execution_plan.yaml"],
            )
            add_edge(
                dependency_id,
                current,
                "plan_depends_on",
                confidence="deterministic",
                evidence_refs=["machine_execution_plan.yaml"],
            )
        for step in phase.steps:
            for manifest_ref in step.manifest_refs:
                normalized = str(manifest_ref).replace("\\", "/")
                identities = path_to_identities.get(normalized, [])
                if not identities:
                    missing = add_synthetic(
                        "ManifestArtifact",
                        normalized,
                        bundle.target_namespace,
                        status="missing",
                        source="unresolved_plan_manifest",
                        evidence_refs=["machine_execution_plan.yaml", normalized],
                    )
                    identities = [missing]
                for identity in identities:
                    add_edge(
                        current,
                        identity,
                        "plan_applies",
                        confidence="deterministic",
                        evidence_refs=["machine_execution_plan.yaml", normalized],
                    )


def _graph_findings(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for node in nodes:
        status = _node_status(node)
        if status not in {"missing", "uncertain"}:
            continue
        evidence = list((node.get("payload_redacted") or {}).get("evidence_refs") or ["bundle"])
        findings.append(
            {
                "finding_id": f"twinfinding_{uuid4().hex}",
                "code": "MISSING_DEPENDENCY" if status == "missing" else "UNCERTAIN_DEPENDENCY",
                "severity": "review",
                "status": "provisional",
                "message": (
                    f"{node['kind']} {node['name']} is {status} in the planned dependency graph."
                ),
                "evidence_refs": evidence,
            }
        )
    if _cycle_count(nodes, edges):
        findings.append(
            {
                "finding_id": f"twinfinding_{uuid4().hex}",
                "code": "DEPENDENCY_CYCLE",
                "severity": "review",
                "status": "provisional",
                "message": "One or more directed dependency cycles require operator review.",
                "evidence_refs": ["machine_execution_plan.yaml", "rendered-manifests"],
            }
        )
    return findings


def _cycle_count(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> int:
    identities = {item["stable_identity"] for item in nodes}
    adjacency: dict[str, set[str]] = {identity: set() for identity in identities}
    for edge in edges:
        if edge["source_identity"] in adjacency and edge["target_identity"] in identities:
            adjacency[edge["source_identity"]].add(edge["target_identity"])
    state: dict[str, int] = {}
    cycles = 0

    def visit(node: str) -> None:
        nonlocal cycles
        state[node] = 1
        for target in adjacency.get(node, set()):
            if state.get(target) == 1:
                cycles += 1
            elif state.get(target, 0) == 0:
                visit(target)
        state[node] = 2

    for identity in sorted(identities):
        if state.get(identity, 0) == 0:
            visit(identity)
    return cycles


def _workload_references(kind: str, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    if kind not in WORKLOAD_KINDS:
        return []
    spec = manifest.get("spec") or {}
    pod_spec = (
        spec
        if kind == "Pod"
        else (
            (spec.get("jobTemplate") or {}).get("spec", {}).get("template", {}).get("spec")
            if kind == "CronJob"
            else (spec.get("template") or {}).get("spec")
        )
    )
    if not isinstance(pod_spec, dict):
        return []
    refs: set[tuple[str, str]] = set()
    if pod_spec.get("serviceAccountName"):
        refs.add(("ServiceAccount", str(pod_spec["serviceAccountName"])))
    for item in pod_spec.get("imagePullSecrets") or []:
        if isinstance(item, dict) and item.get("name"):
            refs.add(("Secret", str(item["name"])))
    for volume in pod_spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        config_map_name = _mapping_value(volume.get("configMap"), "name")
        secret_name = _mapping_value(volume.get("secret"), "secretName")
        claim_name = _mapping_value(volume.get("persistentVolumeClaim"), "claimName")
        if config_map_name:
            refs.add(("ConfigMap", config_map_name))
        if secret_name:
            refs.add(("Secret", secret_name))
        if claim_name:
            refs.add(("PersistentVolumeClaim", claim_name))
        projected = volume.get("projected")
        for source in (projected.get("sources") if isinstance(projected, dict) else []) or []:
            if not isinstance(source, dict):
                continue
            config_map_name = _mapping_value(source.get("configMap"), "name")
            secret_name = _mapping_value(source.get("secret"), "name")
            if config_map_name:
                refs.add(("ConfigMap", config_map_name))
            if secret_name:
                refs.add(("Secret", secret_name))
    for container in list(pod_spec.get("initContainers") or []) + list(
        pod_spec.get("containers") or []
    ):
        if not isinstance(container, dict):
            continue
        for env_from in container.get("envFrom") or []:
            if not isinstance(env_from, dict):
                continue
            config_map_name = _mapping_value(env_from.get("configMapRef"), "name")
            secret_name = _mapping_value(env_from.get("secretRef"), "name")
            if config_map_name:
                refs.add(("ConfigMap", config_map_name))
            if secret_name:
                refs.add(("Secret", secret_name))
        for env in container.get("env") or []:
            value_from = (env or {}).get("valueFrom") if isinstance(env, dict) else None
            if not isinstance(value_from, dict):
                continue
            config_map_ref = value_from.get("configMapKeyRef")
            secret_ref = value_from.get("secretKeyRef")
            if isinstance(config_map_ref, dict) and config_map_ref.get("name"):
                refs.add(("ConfigMap", str(config_map_ref["name"])))
            if isinstance(secret_ref, dict) and secret_ref.get("name"):
                refs.add(("Secret", str(secret_ref["name"])))
    return sorted(refs)


def _mapping_value(value: Any, key: str) -> str | None:
    """Return a named reference only when generated YAML preserved mapping shape."""
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    return str(item) if item else None


def _ingress_backends(manifest: dict[str, Any]) -> list[str]:
    spec = manifest.get("spec") or {}
    names: set[str] = set()
    backends = [spec.get("defaultBackend") or spec.get("backend") or {}]
    for rule in spec.get("rules") or []:
        for path in ((rule or {}).get("http") or {}).get("paths") or []:
            backends.append((path or {}).get("backend") or {})
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        name = (backend.get("service") or {}).get("name") or backend.get("serviceName")
        if name:
            names.add(str(name))
    return sorted(names)


def _pod_template_labels(kind: str, manifest: dict[str, Any]) -> dict[str, str]:
    spec = manifest.get("spec") or {}
    if kind == "Pod":
        labels = (manifest.get("metadata") or {}).get("labels") or {}
    elif kind == "CronJob":
        labels = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get(
            "metadata", {}
        ).get("labels") or {}
    else:
        labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
    return (
        {str(key): str(value) for key, value in labels.items()} if isinstance(labels, dict) else {}
    )


def _helm_release(metadata: dict[str, Any]) -> str | None:
    labels = metadata.get("labels") or {}
    annotations = metadata.get("annotations") or {}
    value = annotations.get("meta.helm.sh/release-name") or labels.get("app.kubernetes.io/instance")
    return str(value) if value else None


def _selector_name(selector: dict[str, Any]) -> str:
    text = ",".join(f"{key}={selector[key]}" for key in sorted(selector))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"selector-{digest}"


def _node_status(node: dict[str, Any]) -> str:
    return str((node.get("payload_redacted") or {}).get("status") or "present")


def _node_risk(kind: str, status: str) -> str:
    if status == "uncertain":
        return "medium"
    if status == "missing" and kind in {
        "CustomResourceDefinition",
        "Secret",
        "Service",
        "ServiceAccount",
        "ClusterRole",
        "Role",
    }:
        return "high"
    if status == "missing":
        return "medium"
    if kind in {"ClusterRole", "CustomResourceDefinition", "Secret"}:
        return "medium"
    return "low"
