"""Target dependency graph with a deterministic topological order."""

from __future__ import annotations

import heapq
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .model import CycleError, GraphError, Target

__all__ = ["DependencyGraph"]


class DependencyGraph:
    """Immutable dependency graph built from a list of :class:`Target` objects.

    Construction validates the graph eagerly:

    * duplicate target names raise :class:`GraphError`,
    * dependencies on unknown targets raise :class:`GraphError`.
    """

    def __init__(self, targets: Iterable[Target]) -> None:
        self.targets: Dict[str, Target] = {}
        for target in targets:
            if target.name in self.targets:
                raise GraphError(f"duplicate target name: {target.name!r}")
            self.targets[target.name] = target

        self._deps: Dict[str, Tuple[str, ...]] = {}
        self._dependents: Dict[str, Set[str]] = {name: set() for name in self.targets}

        for name, target in self.targets.items():
            for dep in target.deps:
                if dep not in self.targets:
                    raise GraphError(
                        f"target {name!r} depends on unknown target {dep!r}"
                    )
            self._deps[name] = tuple(target.deps)

        for name, deps in self._deps.items():
            for dep in deps:
                self._dependents[dep].add(name)

        self._order: List[str] | None = None

    # -- queries ------------------------------------------------------------ #
    def dependencies(self, name: str) -> Tuple[str, ...]:
        return self._deps[name]

    def dependents(self, name: str) -> Tuple[str, ...]:
        """Targets that depend *on* ``name`` (reverse edges), sorted."""
        return tuple(sorted(self._dependents[name]))

    def __contains__(self, name: object) -> bool:
        return name in self.targets

    def __len__(self) -> int:
        return len(self.targets)

    # -- ordering ----------------------------------------------------------- #
    def topological_order(self) -> List[str]:
        """Dependencies always appear before the targets that need them.

        Ties are broken alphabetically so the order (and therefore the logs and
        benchmarks) are reproducible across runs.
        """
        if self._order is None:
            self._order = self._compute_order()
        return list(self._order)

    def _compute_order(self) -> List[str]:
        indegree = {name: len(self._deps[name]) for name in self.targets}
        ready = [name for name, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)

        order: List[str] = []
        while ready:
            node = heapq.heappop(ready)
            order.append(node)
            for dependent in sorted(self._dependents[node]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)

        if len(order) != len(self.targets):
            unresolved = sorted(set(self.targets) - set(order))
            raise CycleError(
                "dependency cycle detected involving: " + ", ".join(unresolved)
            )
        return order

    # -- selection ---------------------------------------------------------- #
    def closure(self, names: Sequence[str]) -> Set[str]:
        """``names`` plus every transitive dependency."""
        seen: Set[str] = set()
        stack = list(names)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            if name not in self.targets:
                raise GraphError(f"unknown target: {name!r}")
            seen.add(name)
            stack.extend(self._deps[name])
        return seen

    def order_for(self, names: Sequence[str]) -> List[str]:
        """Topological order restricted to ``names`` and their dependencies."""
        wanted = self.closure(names)
        return [name for name in self.topological_order() if name in wanted]

    # -- rendering ---------------------------------------------------------- #
    def describe(self) -> str:
        """A short textual listing of targets grouped by build stage."""
        lines = []
        for index, name in enumerate(self.topological_order()):
            target = self.targets[name]
            deps = ", ".join(target.deps) or "-"
            outputs = ", ".join(target.outputs) or "-"
            lines.append(
                f"{index:>3}. {name:<24} deps[{deps}]  outputs[{outputs}]"
            )
        return "\n".join(lines)

    def to_dot(self) -> str:
        """Graphviz DOT rendering (``dep -> target`` edges)."""
        lines = ["digraph buildcache {", "  rankdir=LR;"]
        for name in self.targets:
            lines.append(f'  "{name}";')
        for name, deps in self._deps.items():
            for dep in deps:
                lines.append(f'  "{dep}" -> "{name}";')
        lines.append("}")
        return "\n".join(lines)
