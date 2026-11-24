"""End-to-end tests for buildcache.engine (the invalidation logic)."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from buildcache.engine import BuildEngine
from buildcache.model import BuildError, CommandError, Status, Target

TOOL_SOURCE = '''\
"""Tiny helper used by the tests; mode is the first argument."""
import sys
from pathlib import Path

mode = sys.argv[1]
args = sys.argv[2:]

if mode == "upper":
    src, dst = args
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(Path(src).read_text(encoding="utf-8").upper(), encoding="utf-8")
elif mode == "concat":
    *srcs, dst = args
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text("".join(Path(s).read_text(encoding="utf-8") for s in srcs), encoding="utf-8")
elif mode == "noop":
    pass
elif mode == "fail":
    sys.exit(3)
else:
    sys.exit("unknown mode: " + mode)
'''


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tool.py").write_text(TOOL_SOURCE, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------- #
    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def cmd(self, *args: str) -> list:
        return [sys.executable, "tool.py", *args]

    def engine(self, targets, **kwargs) -> BuildEngine:
        kwargs.setdefault("stream", io.StringIO())
        return BuildEngine(targets, self.root, **kwargs)

    def simple_chain(self):
        """a -> b -> c, each uppercasing the previous output."""
        self.write("a.txt", "alpha")
        return [
            Target(name="a", command=self.cmd("upper", "a.txt", "out/a.txt"),
                   inputs=["a.txt", "tool.py"], outputs=["out/a.txt"]),
            Target(name="b", command=self.cmd("upper", "out/a.txt", "out/b.txt"),
                   inputs=["tool.py"], outputs=["out/b.txt"], deps=["a"]),
            Target(name="c", command=self.cmd("upper", "out/b.txt", "out/c.txt"),
                   inputs=["tool.py"], outputs=["out/c.txt"], deps=["b"]),
        ]


class BasicBuildTests(EngineTestCase):
    def test_cold_build_reports_all_misses(self) -> None:
        report = self.engine(self.simple_chain()).build()
        self.assertEqual(report.total, 3)
        self.assertEqual(report.rebuilt, 3)
        self.assertEqual(report.hits, 0)
        self.assertEqual((self.root / "out/c.txt").read_text(encoding="utf-8"), "ALPHA")

    def test_warm_build_is_all_cache_hits(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        report = self.engine(targets).build()  # fresh engine -> reloads cache from disk
        self.assertEqual(report.hits, 3)
        self.assertEqual(report.rebuilt, 0)

    def test_force_rebuilds_everything(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        report = self.engine(targets, force=True).build()
        self.assertEqual(report.forced, 3)
        self.assertEqual(report.hits, 0)


class InvalidationTests(EngineTestCase):
    def test_input_change_invalidates_transitively(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        self.write("a.txt", "alpha-changed")

        report = self.engine(targets).build()
        self.assertEqual(report.rebuilt, 3)
        self.assertEqual((self.root / "out/c.txt").read_text(encoding="utf-8"), "ALPHA-CHANGED")

    def test_explain_names_the_changed_input(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        self.write("a.txt", "alpha-changed")
        report = self.engine(targets).build()
        reasons = {r.name: r.reason for r in report.results}
        self.assertIn("input changed: a.txt", reasons["a"])
        self.assertIn("dependency a changed", reasons["b"])

    def test_timestamp_only_change_is_a_cache_hit(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        self.write("a.txt", "alpha")  # same bytes, new mtime
        report = self.engine(targets).build()
        self.assertEqual(report.rebuilt, 0)

    def test_externally_modified_output_is_detected(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        (self.root / "out/a.txt").write_text("TAMPERED", encoding="utf-8")

        report = self.engine(targets).build()
        reasons = {r.name: r.reason for r in report.results}
        self.assertIs(report.results[0].status, Status.MISS)
        self.assertIn("output missing or modified externally", reasons["a"])

    def test_deleted_output_is_rebuilt(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        (self.root / "out/c.txt").unlink()
        report = self.engine(targets).build()
        self.assertEqual([r.name for r in report.results if r.rebuilt], ["c"])

    def test_command_change_invalidates(self) -> None:
        targets = self.simple_chain()
        self.engine(targets).build()
        changed = self.simple_chain()
        changed[0] = Target(name="a", command=self.cmd("upper", "./a.txt", "out/a.txt"),
                            inputs=["a.txt", "tool.py"], outputs=["out/a.txt"])
        report = self.engine(changed).build()
        self.assertIn("command changed", {r.name: r.reason for r in report.results}["a"])

    def test_env_change_invalidates(self) -> None:
        self.write("a.txt", "alpha")
        base = Target(name="a", command=self.cmd("upper", "a.txt", "out/a.txt"),
                      inputs=["a.txt", "tool.py"], outputs=["out/a.txt"], env={"MODE": "1"})
        self.engine([base]).build()
        variant = Target(name="a", command=self.cmd("upper", "a.txt", "out/a.txt"),
                         inputs=["a.txt", "tool.py"], outputs=["out/a.txt"], env={"MODE": "2"})
        report = self.engine([variant]).build()
        self.assertIn("environment changed", {r.name: r.reason for r in report.results}["a"])


class PhonyTests(EngineTestCase):
    def test_phony_target_skips_and_propagates_dependency_digest(self) -> None:
        self.write("a.txt", "alpha")
        targets = [
            Target(name="a", command=self.cmd("upper", "a.txt", "out/a.txt"),
                   inputs=["a.txt", "tool.py"], outputs=["out/a.txt"]),
            Target(name="group", deps=["a"], phony=True),
            Target(name="b", command=self.cmd("upper", "out/a.txt", "out/b.txt"),
                   inputs=["tool.py"], outputs=["out/b.txt"], deps=["group"]),
        ]
        first = self.engine(targets).build()
        self.assertIs(first.results[1].status, Status.SKIPPED)

        # Nothing changed -> everything cached.
        second = self.engine(targets).build()
        self.assertEqual(second.rebuilt, 0)

        # Changing `a.txt` must invalidate `b` even though b only depends on the
        # phony group, because the group's digest is derived from its deps.
        self.write("a.txt", "alpha-changed")
        third = self.engine(targets).build()
        self.assertEqual({r.name for r in third.results if r.rebuilt}, {"a", "b"})


class SelectionTests(EngineTestCase):
    def test_building_one_target_builds_only_its_dependencies(self) -> None:
        targets = self.simple_chain()
        self.write("d.txt", "delta")
        targets.append(
            Target(name="d", command=self.cmd("upper", "d.txt", "out/d.txt"),
                   inputs=["d.txt", "tool.py"], outputs=["out/d.txt"])
        )
        report = self.engine(targets).build(selected=["c"])
        self.assertEqual({r.name for r in report.results}, {"a", "b", "c"})
        self.assertFalse((self.root / "out/d.txt").exists())


class PreviewTests(EngineTestCase):
    def test_preview_executes_nothing_but_predicts(self) -> None:
        targets = self.simple_chain()
        preview = self.engine(targets).preview()
        self.assertTrue(all(r.status is Status.MISS for r in preview))
        self.assertFalse((self.root / "out/a.txt").exists())  # nothing ran

        self.engine(targets).build()
        self.write("a.txt", "alpha-changed")
        preview = self.engine(targets).preview()
        self.assertEqual({r.name for r in preview if r.status is Status.MISS}, {"a", "b", "c"})


class ErrorTests(EngineTestCase):
    def test_missing_input_raises(self) -> None:
        targets = [Target(name="a", command=self.cmd("upper", "missing.txt", "out/a.txt"),
                          inputs=["missing.txt"], outputs=["out/a.txt"])]
        with self.assertRaises(BuildError):
            self.engine(targets).build()

    def test_declared_output_not_produced_raises(self) -> None:
        self.write("a.txt", "alpha")
        targets = [Target(name="a", command=self.cmd("noop"),
                          inputs=["a.txt"], outputs=["out/a.txt"])]
        with self.assertRaises(BuildError):
            self.engine(targets).build()

    def test_failing_command_raises_and_does_not_cache(self) -> None:
        targets = [Target(name="a", command=self.cmd("fail"), inputs=[], outputs=[])]
        engine = self.engine(targets)
        with self.assertRaises(CommandError):
            engine.build()
        self.assertEqual(len(engine.cache), 0)

    def test_corrupt_cache_does_not_break_build(self) -> None:
        targets = self.simple_chain()
        cache_dir = self.root / ".buildcache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "cache.json").write_text("not json at all", encoding="utf-8")
        report = self.engine(targets).build()
        self.assertEqual(report.rebuilt, 3)


if __name__ == "__main__":
    unittest.main()
