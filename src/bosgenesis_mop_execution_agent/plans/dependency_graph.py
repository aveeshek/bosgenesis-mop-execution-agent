"""Dependency graph validation."""

from __future__ import annotations

from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan


class DependencyGraphError(ValueError):
    """Raised when a dependency graph is invalid."""


def validate_dependency_graph(plan: MachineExecutionPlan) -> None:
    """Validate known phase references and reject cycles."""
    graph = _phase_graph(plan)
    _validate_known_dependencies(graph)
    _detect_cycles(graph)


def _phase_graph(plan: MachineExecutionPlan) -> dict[str, set[str]]:
    graph = {phase.phase_id: set(phase.depends_on) for phase in plan.phases}
    for entry in plan.dependency_graph:
        graph.setdefault(entry.phase_id, set()).update(entry.depends_on)
    return graph


def _validate_known_dependencies(graph: dict[str, set[str]]) -> None:
    known = set(graph)
    for phase_id, dependencies in graph.items():
        missing = dependencies - known
        if missing:
            missing_text = ",".join(sorted(missing))
            raise DependencyGraphError(f"unknown_phase_dependency:{phase_id}:{missing_text}")


def _detect_cycles(graph: dict[str, set[str]]) -> None:
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise DependencyGraphError(f"phase_dependency_cycle:{node}")
        temporary.add(node)
        for dependency in graph[node]:
            visit(dependency)
        temporary.remove(node)
        permanent.add(node)

    for phase_id in graph:
        visit(phase_id)
