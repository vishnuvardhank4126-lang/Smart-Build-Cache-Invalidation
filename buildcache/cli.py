"""Command line interface: ``python -m buildcache <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .cache import CacheStore
from .config import find_build_file, load_targets
from .engine import BuildEngine, BuildReport
from .graph import DependencyGraph
from .model import BuildError, Status

__all__ = ["main", "build_parser"]

_MARK = {
    Status.HIT: "cache hit",
    Status.MISS: "rebuilt  ",
    Status.FORCED: "forced   ",
    Status.SKIPPED: "skipped  ",
}


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--project", default=".", help="project directory (default: .)")
    parser.add_argument("-f", "--build-file", default=None, help="path to the build file")
    parser.add_argument(
        "-c", "--cache-dir", default=None,
        help="cache directory (default: <project>/.buildcache)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="stream command output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buildcache",
        description=(
            "Content-addressed incremental build engine. Rebuilds only the targets "
            "whose inputs, dependencies, command or environment actually changed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="build targets, reusing valid cache entries")
    _add_common(p_build)
    p_build.add_argument("targets", nargs="*", help="targets to build (default: all)")
    p_build.add_argument("--force", action="store_true", help="ignore the cache and re-run everything")
    p_build.add_argument("--json", action="store_true", help="emit the report as JSON")
    p_build.set_defaults(func=cmd_build)

    p_explain = sub.add_parser("explain", help="show what would rebuild, and why, without running it")
    _add_common(p_explain)
    p_explain.add_argument("targets", nargs="*", help="targets to analyse (default: all)")
    p_explain.set_defaults(func=cmd_explain)

    p_status = sub.add_parser("status", help="show the cached state of each target")
    _add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_graph = sub.add_parser("graph", help="print the build graph / topological order")
    _add_common(p_graph)
    p_graph.add_argument("--dot", action="store_true", help="emit Graphviz DOT instead of a listing")
    p_graph.set_defaults(func=cmd_graph)

    p_clean = sub.add_parser("clean", help="delete the cache (and optionally the outputs)")
    _add_common(p_clean)
    p_clean.add_argument("--outputs", action="store_true", help="also delete declared output files")
    p_clean.set_defaults(func=cmd_clean)

    return parser


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_project(args: argparse.Namespace):
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise BuildError(f"project directory not found: {project}")
    build_file = (
        Path(args.build_file).expanduser().resolve()
        if args.build_file
        else find_build_file(project)
    )
    targets = load_targets(build_file, project)
    return project, build_file, targets


def _print_report(report: BuildReport) -> None:
    width = max((len(r.name) for r in report.results), default=8)
    for result in report.results:
        line = f"  {_MARK[result.status]}  {result.name:<{width}}  {result.duration:6.3f}s"
        if result.rebuilt and result.reason:
            line += f"  <- {result.reason}"
        print(line)
    print(
        f"\n{report.total} targets: {report.hits} cached, {report.rebuilt} rebuilt "
        f"in {report.duration:.3f}s"
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    project, _, targets = _load_project(args)
    engine = BuildEngine(
        targets, project, cache_dir=args.cache_dir, force=args.force, verbose=args.verbose
    )
    report = engine.build(selected=args.targets or None)
    if args.json:
        print(json.dumps(report.summary_dict(), indent=2))
    else:
        _print_report(report)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    project, _, targets = _load_project(args)
    engine = BuildEngine(targets, project, cache_dir=args.cache_dir, verbose=args.verbose)
    results = engine.preview(selected=args.targets or None)
    if not results:
        print("no targets selected")
        return 0
    width = max(len(r.name) for r in results)
    print("plan (no commands executed):")
    verdicts = {Status.MISS: "would rebuild", Status.HIT: "would reuse  ", Status.SKIPPED: "would skip   "}
    for result in results:
        verdict = verdicts.get(result.status, result.status.value.lower())
        note = f"  <- {result.reason}" if result.reason and result.status is Status.MISS else ""
        print(f"  {verdict}  {result.name:<{width}}{note}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project, _, targets = _load_project(args)
    cache_dir = Path(args.cache_dir) if args.cache_dir else project / ".buildcache"
    store = CacheStore(cache_dir).load()
    if not len(store):
        print(f"cache is empty ({store.path})")
        return 0
    order = DependencyGraph(targets).topological_order()
    print(f"cache: {store.path}")
    for name in order:
        record = store.get(name)
        if record is None:
            print(f"  {name:<24} (never built)")
            continue
        print(
            f"  {name:<24} action={record.action_hash[:12]}  "
            f"out={record.output_digest[:12]}  {record.duration:6.3f}s  "
            f"{record.status}"
        )
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    _, _, targets = _load_project(args)
    graph = DependencyGraph(targets)
    if args.dot:
        print(graph.to_dot())
    else:
        print(f"build order ({len(graph)} targets):")
        print(graph.describe())
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    project, _, targets = _load_project(args)
    cache_dir = Path(args.cache_dir) if args.cache_dir else project / ".buildcache"
    store = CacheStore(cache_dir).load()
    removed = store.clear()
    store.save()

    deleted_outputs = 0
    if args.outputs:
        for target in targets:
            cwd = (project / target.cwd) if target.cwd else project
            for output in target.outputs:
                path = cwd / output
                if path.is_dir():
                    continue
                if path.exists():
                    path.unlink()
                    deleted_outputs += 1
    print(
        f"cleared {removed} cache entr{'y' if removed == 1 else 'ies'}"
        + (f", deleted {deleted_outputs} output file(s)" if args.outputs else "")
    )
    return 0


# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
