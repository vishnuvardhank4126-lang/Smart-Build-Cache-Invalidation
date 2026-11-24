"""The incremental build engine.

The decision procedure for every target is:

1. Collect the *input hashes* (content digests of the declared input files).
2. Collect the *dependency digests* (output digest of each upstream target).
3. Build the **action hash** from command + cwd + env + inputs + deps + an
   environment fingerprint.
4. If a cache record exists, its action hash matches, and every declared output
   still hashes to the recorded value -> **cache hit**, execute nothing.
5. Otherwise run the command, hash the produced outputs and store a new record.

Step 4 is what makes the invalidation "smart": a change is only propagated as
far as it actually affects results, and stale or externally modified outputs are
detected instead of silently trusted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, TextIO, Union

from .cache import CacheStore
from .graph import DependencyGraph
from .hashing import combine_hashes, environment_fingerprint, hash_path, hash_string
from .model import BuildError, CacheRecord, CommandError, Status, Target

__all__ = ["BuildEngine", "BuildResult", "BuildReport"]


def _norm(path: Union[str, os.PathLike]) -> str:
    """Normalise a path for use as a stable cache key."""
    return os.fspath(path).replace(os.sep, "/")


@dataclass
class BuildResult:
    """What happened to a single target."""

    name: str
    status: Status
    reason: str = ""
    duration: float = 0.0
    output_digest: str = ""
    action_hash: str = ""

    @property
    def rebuilt(self) -> bool:
        return self.status in (Status.MISS, Status.FORCED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "duration": round(self.duration, 6),
            "output_digest": self.output_digest,
            "action_hash": self.action_hash,
        }


@dataclass
class BuildReport:
    """Aggregate result of one :meth:`BuildEngine.build` call."""

    results: List[BuildResult]
    duration: float
    cache_dir: str

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hits(self) -> int:
        return sum(1 for r in self.results if r.status is Status.HIT)

    @property
    def misses(self) -> int:
        return sum(1 for r in self.results if r.status is Status.MISS)

    @property
    def forced(self) -> int:
        return sum(1 for r in self.results if r.status is Status.FORCED)

    @property
    def rebuilt(self) -> int:
        return sum(1 for r in self.results if r.rebuilt)

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "hits": self.hits,
            "misses": self.misses,
            "forced": self.forced,
            "rebuilt": self.rebuilt,
            "duration": round(self.duration, 6),
            "cache_dir": self.cache_dir,
            "targets": [r.to_dict() for r in self.results],
        }


class BuildEngine:
    """Runs a target graph with content-addressed cache invalidation."""

    def __init__(
        self,
        targets: Sequence[Target],
        project_dir: Union[str, Path],
        cache_dir: Optional[Union[str, Path]] = None,
        *,
        force: bool = False,
        verbose: bool = False,
        stream: Optional[TextIO] = None,
        cache: Optional[CacheStore] = None,
    ) -> None:
        self.targets = list(targets)
        self.project_dir = Path(project_dir).resolve()
        self.cache_dir = Path(cache_dir) if cache_dir else self.project_dir / ".buildcache"
        self.graph = DependencyGraph(self.targets)
        self.cache = cache if cache is not None else CacheStore(self.cache_dir)
        self.force = force
        self.verbose = verbose
        self.stream = stream if stream is not None else sys.stdout
        self._results: Dict[str, BuildResult] = {}

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def build(self, selected: Optional[Sequence[str]] = None) -> BuildReport:
        """Build ``selected`` targets (default: all) and return a report."""
        self.cache.load()
        for warning in self.cache.warnings:
            self._log(f"warning: {warning}")

        order = (
            self.graph.order_for(selected) if selected else self.graph.topological_order()
        )
        self._log(f"building {len(order)} target(s) from {self.project_dir}")
        started = time.perf_counter()
        results: List[BuildResult] = []
        try:
            for name in order:
                results.append(self._build_target(self.graph.targets[name]))
        finally:
            self.cache.save()
        report = BuildReport(results, time.perf_counter() - started, str(self.cache_dir))
        self._log(
            f"done: {report.hits} hit, {report.rebuilt} rebuilt, "
            f"{report.duration:.3f}s"
        )
        return report

    def preview(self, selected: Optional[Sequence[str]] = None) -> List[BuildResult]:
        """Predict what a build *would* do, without executing any command.

        Used by ``buildcache explain``.  Because nothing is executed, the output
        digest of a target that is predicted to rebuild is unknown; downstream
        targets are reported as "dependent will rebuild".
        """
        self.cache.load()
        order = (
            self.graph.order_for(selected) if selected else self.graph.topological_order()
        )
        planned: Dict[str, str] = {}
        results: List[BuildResult] = []
        for name in order:
            target = self.graph.targets[name]
            dep_digests = {dep: planned.get(dep, "") for dep in target.deps}
            if target.command is None:
                digest = combine_hashes(dep_digests)
                results.append(
                    BuildResult(name, Status.SKIPPED, "phony target (no command)",
                                output_digest=digest)
                )
                planned[name] = digest
                continue
            try:
                cwd, input_hashes, dep_hashes, env_fp, action_hash = self._collect(
                    target, dep_digests
                )
            except BuildError as exc:
                results.append(BuildResult(name, Status.MISS, str(exc)))
                planned[name] = ""
                continue

            record = self.cache.get(name)
            if (
                not self.force
                and record
                and record.action_hash == action_hash
                and self._outputs_valid(record, target, cwd)
            ):
                results.append(
                    BuildResult(name, Status.HIT, "up to date", output_digest=record.output_digest,
                                action_hash=action_hash)
                )
                planned[name] = record.output_digest
            else:
                reason = (
                    "forced rebuild"
                    if self.force
                    else self._explain(record, target, input_hashes, dep_hashes, env_fp, action_hash)
                )
                results.append(
                    BuildResult(name, Status.MISS, reason,
                                output_digest=record.output_digest if record else "",
                                action_hash=action_hash)
                )
                planned[name] = "<will change>"
        return results

    # ------------------------------------------------------------------ #
    # Per-target logic
    # ------------------------------------------------------------------ #
    def _build_target(self, target: Target) -> BuildResult:
        dep_digests = {dep: self._results[dep].output_digest for dep in target.deps}
        cwd, input_hashes, dep_hashes, env_fp, action_hash = self._collect(target, dep_digests)

        record = self.cache.get(target.name)
        if (
            not self.force
            and record is not None
            and record.action_hash == action_hash
            and self._outputs_valid(record, target, cwd)
        ):
            self._log(f"  {Status.HIT.value:<7} {target.name} (up to date)")
            result = BuildResult(
                target.name, Status.HIT, "cache entry reused",
                output_digest=record.output_digest, action_hash=action_hash,
            )
            self._results[target.name] = result
            return result

        if target.command is None:
            # Phony grouping target: its identity is the digest of its deps, so
            # changing a dependency still invalidates anything that depends on it.
            digest = combine_hashes(dep_hashes)
            result = BuildResult(
                target.name, Status.SKIPPED, "phony target (no command)",
                output_digest=digest, action_hash=action_hash,
            )
            self._log(f"  {Status.SKIPPED.value:<7} {target.name} (phony)")
            self._results[target.name] = result
            return result

        reason = (
            "forced rebuild"
            if self.force
            else self._explain(record, target, input_hashes, dep_hashes, env_fp, action_hash)
        )
        status = Status.FORCED if self.force else Status.MISS

        self._log(f"  {status.value:<7} {target.name} ({reason})")
        started = time.perf_counter()
        self._execute(target, cwd)
        duration = time.perf_counter() - started

        output_hashes = self._collect_outputs(target, cwd)
        output_digest = combine_hashes(output_hashes)

        self.cache.put(
            CacheRecord(
                name=target.name,
                action_hash=action_hash,
                status=status.value,
                command=target.command_key,
                input_hashes=input_hashes,
                dep_hashes=dep_hashes,
                output_hashes=output_hashes,
                output_digest=output_digest,
                env_fingerprint=env_fp,
                timestamp=time.time(),
                duration=duration,
            )
        )
        result = BuildResult(
            target.name, status, reason,
            duration=duration, output_digest=output_digest, action_hash=action_hash,
        )
        self._results[target.name] = result
        return result

    # ------------------------------------------------------------------ #
    # Hashing helpers
    # ------------------------------------------------------------------ #
    def _collect(
        self, target: Target, dep_digests: Dict[str, str]
    ) -> tuple[Path, Dict[str, str], Dict[str, str], str, str]:
        cwd = (self.project_dir / target.cwd).resolve() if target.cwd else self.project_dir
        if not cwd.is_dir():
            raise BuildError(f"target {target.name!r}: working directory not found: {cwd}")

        input_hashes: Dict[str, str] = {}
        for pattern in target.inputs:
            matches = self._expand(cwd, pattern)
            if not matches:
                raise BuildError(
                    f"target {target.name!r}: declared input {pattern!r} does not exist"
                )
            for path in matches:
                key = _norm(os.path.relpath(path, cwd))
                input_hashes[key] = hash_path(path)

        dep_hashes = {dep: dep_digests.get(dep, "") for dep in target.deps}
        env_fp = environment_fingerprint(target.env)
        action_hash = self._action_hash(target, input_hashes, dep_hashes, env_fp)
        return cwd, input_hashes, dep_hashes, env_fp, action_hash

    @staticmethod
    def _action_hash(
        target: Target,
        input_hashes: Dict[str, str],
        dep_hashes: Dict[str, str],
        env_fp: str,
    ) -> str:
        payload = {
            "name": target.name,
            "command": target.command_key,
            "cwd": target.cwd or "",
            "env": sorted(target.env.items()),
            "inputs": sorted(input_hashes.items()),
            "deps": sorted(dep_hashes.items()),
            "env_fingerprint": env_fp,
        }
        return hash_string(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _expand(cwd: Path, pattern: str) -> List[Path]:
        if any(ch in pattern for ch in "*?["):
            return sorted(p for p in cwd.glob(pattern) if p.is_file())
        path = cwd / pattern
        return [path] if path.exists() else []

    @staticmethod
    def _collect_outputs(target: Target, cwd: Path) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        for output in target.outputs:
            path = cwd / output
            if not path.exists():
                raise BuildError(
                    f"target {target.name!r} finished but declared output "
                    f"{output!r} was not produced"
                )
            hashes[_norm(output)] = hash_path(path)
        return hashes

    @staticmethod
    def _outputs_valid(record: CacheRecord, target: Target, cwd: Path) -> bool:
        """Are the previously produced outputs still intact on disk?"""
        for output in target.outputs:
            key = _norm(output)
            recorded = record.output_hashes.get(key)
            path = cwd / output
            if recorded is None or not path.exists():
                return False
            if hash_path(path) != recorded:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Explanation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _explain(
        record: Optional[CacheRecord],
        target: Target,
        input_hashes: Dict[str, str],
        dep_hashes: Dict[str, str],
        env_fp: str,
        action_hash: str,
    ) -> str:
        """Human-readable reason why ``target`` is not a cache hit."""
        if record is None:
            return "no cache entry yet (first build)"

        reasons: List[str] = []
        if record.command != target.command_key:
            reasons.append("command changed")
        if record.env_fingerprint != env_fp:
            reasons.append("environment changed")

        old_inputs, new_inputs = record.input_hashes, input_hashes
        for path in sorted(set(new_inputs) - set(old_inputs)):
            reasons.append(f"new input {path}")
        for path in sorted(set(old_inputs) - set(new_inputs)):
            reasons.append(f"removed input {path}")
        for path in sorted(set(old_inputs) & set(new_inputs)):
            if old_inputs[path] != new_inputs[path]:
                reasons.append(f"input changed: {path}")

        for dep in sorted(set(record.dep_hashes) | set(dep_hashes)):
            if record.dep_hashes.get(dep) != dep_hashes.get(dep):
                reasons.append(f"dependency {dep} changed")

        if not reasons:
            if record.action_hash == action_hash:
                reasons.append("output missing or modified externally")
            else:
                reasons.append("action hash changed")
        return "; ".join(reasons)

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def _execute(self, target: Target, cwd: Path) -> None:
        env = os.environ.copy()
        env.update(target.env)
        env["BUILDCACHE_TARGET"] = target.name
        env["BUILDCACHE_PROJECT"] = str(self.project_dir)

        kwargs: Dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "text": True,
        }
        if self.verbose:
            completed = subprocess.run(  # type: ignore[call-overload]
                target.command,
                shell=target.shell,
                **kwargs,
            )
        else:
            completed = subprocess.run(  # type: ignore[call-overload]
                target.command,
                shell=target.shell,
                capture_output=True,
                **kwargs,
            )
        if completed.returncode != 0:
            detail = ""
            if not self.verbose:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = (stderr or stdout)[-800:]
            raise CommandError(
                f"target {target.name!r} failed with exit code {completed.returncode}"
                + (f"\n{detail}" if detail else "")
            )

    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:  # pragma: no cover - cosmetic
        print(message, file=self.stream)
