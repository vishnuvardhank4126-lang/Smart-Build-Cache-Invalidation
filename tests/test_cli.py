"""Tests for buildcache.cli."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from buildcache.cli import main

BUILD_FILE = '''\
import sys
from buildcache import Target

TARGETS = [
    Target(
        name="a",
        command=[sys.executable, "tool.py", "upper", "a.txt", "out/a.txt"],
        inputs=["a.txt", "tool.py"],
        outputs=["out/a.txt"],
    ),
    Target(name="all", deps=["a"], phony=True),
]
'''

TOOL = (
    "import pathlib, sys\n"
    "mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True)\n"
    "pathlib.Path(dst).write_text(pathlib.Path(src).read_text().upper())\n"
)


def run(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "build.py").write_text(BUILD_FILE, encoding="utf-8")
        (self.root / "tool.py").write_text(TOOL, encoding="utf-8")
        (self.root / "a.txt").write_text("alpha", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def p(self) -> str:
        return str(self.root)


class CliTests(CliTestCase):
    def test_graph_lists_targets(self) -> None:
        code, out, _ = run("graph", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("a", out)
        self.assertIn("all", out)

    def test_graph_dot(self) -> None:
        code, out, _ = run("graph", "--dot", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("digraph", out)

    def test_build_then_second_build_is_cached(self) -> None:
        code, out, _ = run("build", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("rebuilt", out)
        self.assertTrue((self.root / "out/a.txt").exists())

        code, out, _ = run("build", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("1 cached", out)

    def test_build_json_report(self) -> None:
        run("build", "-p", self.p())
        code, out, _ = run("build", "--json", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn('"hits": 1', out)

    def test_explain_executes_nothing(self) -> None:
        code, out, _ = run("explain", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("would rebuild", out)
        self.assertFalse((self.root / "out/a.txt").exists())

    def test_status_after_build(self) -> None:
        run("build", "-p", self.p())
        code, out, _ = run("status", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("action=", out)

    def test_clean_removes_cache_and_outputs(self) -> None:
        run("build", "-p", self.p())
        code, out, _ = run("clean", "--outputs", "-p", self.p())
        self.assertEqual(code, 0)
        self.assertIn("cleared", out)
        self.assertFalse((self.root / "out/a.txt").exists())

    def test_missing_project_returns_error_code(self) -> None:
        code, _, err = run("build", "-p", str(self.root / "does-not-exist"))
        self.assertEqual(code, 2)
        self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
