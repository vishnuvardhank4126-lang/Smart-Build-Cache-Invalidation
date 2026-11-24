"""Tests for buildcache.graph."""

from __future__ import annotations

import unittest

from buildcache.graph import DependencyGraph
from buildcache.model import CycleError, GraphError, Target


def chain_graph() -> DependencyGraph:
    return DependencyGraph(
        [
            Target(name="c", deps=["b"]),
            Target(name="a"),
            Target(name="b", deps=["a"]),
            Target(name="d"),
        ]
    )


class TopologicalOrderTests(unittest.TestCase):
    def test_dependencies_come_first(self) -> None:
        order = chain_graph().topological_order()
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("c"))

    def test_order_is_deterministic_and_alphabetical_for_ties(self) -> None:
        self.assertEqual(chain_graph().topological_order(), ["a", "b", "c", "d"])
        self.assertEqual(chain_graph().topological_order(), ["a", "b", "c", "d"])

    def test_cycle_is_detected(self) -> None:
        graph = DependencyGraph(
            [Target(name="x", deps=["y"]), Target(name="y", deps=["x"])]
        )
        with self.assertRaises(CycleError):
            graph.topological_order()

    def test_self_dependency_is_a_cycle(self) -> None:
        graph = DependencyGraph([Target(name="x", deps=["x"])])
        with self.assertRaises(CycleError):
            graph.topological_order()


class ValidationTests(unittest.TestCase):
    def test_unknown_dependency_rejected(self) -> None:
        with self.assertRaises(GraphError):
            DependencyGraph([Target(name="a", deps=["ghost"])])

    def test_duplicate_name_rejected(self) -> None:
        with self.assertRaises(GraphError):
            DependencyGraph([Target(name="a"), Target(name="a")])


class SelectionTests(unittest.TestCase):
    def test_closure_pulls_transitive_dependencies(self) -> None:
        self.assertEqual(chain_graph().closure(["c"]), {"a", "b", "c"})

    def test_order_for_restricts_and_orders(self) -> None:
        self.assertEqual(chain_graph().order_for(["c"]), ["a", "b", "c"])

    def test_order_for_unknown_target_raises(self) -> None:
        with self.assertRaises(GraphError):
            chain_graph().order_for(["ghost"])


class RenderingTests(unittest.TestCase):
    def test_dependents_are_reverse_edges(self) -> None:
        self.assertEqual(chain_graph().dependents("a"), ("b",))

    def test_dot_contains_edges(self) -> None:
        dot = chain_graph().to_dot()
        self.assertIn('"a" -> "b"', dot)
        self.assertTrue(dot.startswith("digraph"))

    def test_describe_lists_every_target(self) -> None:
        text = chain_graph().describe()
        for name in ("a", "b", "c", "d"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
