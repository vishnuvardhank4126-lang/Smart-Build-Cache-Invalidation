"""Benchmark: cold vs. warm vs. incremental builds.

Builds a synthetic project made of ``--chains`` independent chains, each
``--length`` targets long, plus one final ``merge`` target.  Each worker step
burns a configurable amount of CPU so the timings are meaningful.

Run::

    python benchmark.py
    python benchmark.py --chains 6 --length 6 --work 20000

Output shows, for each phase, how many targets were served from the cache and
how many were actually re-executed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from buildcache import BuildEngine, Target  # noqa: E402

WORKER_SOURCE = '''\
"""Generated worker: derives one output file from one or more inputs."""
import argparse
import hashlib

parser = argparse.ArgumentParser()
parser.add_argument("--work", type=int, default=10000)
parser.add_argument("--out", required=True)
parser.add_argument("inputs", nargs="+")
args = parser.parse_args()

data = b""
for name in args.inputs:
    with open(name, "rb") as handle:
        data += handle.read()

digest = hashlib.sha256(data).digest()
for _ in range(args.work):          # simulate real compile/analysis cost
    digest = hashlib.sha256(digest).digest()

with open(args.out, "w", encoding="utf-8") as handle:
    handle.write(digest.hex())
'''


def make_project(root: Path, chains: int, length: int, work: int) -> List[Target]:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "worker.py").write_text(WORKER_SOURCE, encoding="utf-8")
    for c in range(chains):
        (root / "src" / f"chain{c}.txt").write_text(
            f"seed for chain {c}\n" * 20, encoding="utf-8"
        )

    targets: List[Target] = []
    tips: List[str] = []
    for c in range(chains):
        previous: str | None = None
        for k in range(length):
            name = f"chain{c}-step{k}"
            source = f"src/chain{c}.txt" if k == 0 else f"out/{previous}.txt"
            output = f"out/{name}.txt"
            targets.append(
                Target(
                    name=name,
                    command=["python", "worker.py", "--work", str(work),
                             "--out", output, source],
                    inputs=[source, "worker.py"],
                    outputs=[output],
                    deps=[previous] if previous else [],
                )
            )
            previous = name
        tips.append(previous)  # type: ignore[arg-type]

    merge_inputs = [f"out/{tip}.txt" for tip in tips]
    targets.append(
        Target(
            name="merge",
            command=["python", "worker.py", "--work", str(work),
                     "--out", "out/final.txt", *merge_inputs],
            inputs=["worker.py"],
            outputs=["out/final.txt"],
            deps=tips,
        )
    )
    return targets


def phase(label: str, engine: BuildEngine) -> dict:
    start = time.perf_counter()
    report = engine.build()
    wall = time.perf_counter() - start
    print(
        f"  {label:<26} {report.total:>3} targets  "
        f"{report.hits:>3} cached  {report.rebuilt:>3} executed  {wall:6.3f}s"
    )
    return {
        "label": label,
        "total": report.total,
        "cached": report.hits,
        "executed": report.rebuilt,
        "wall": wall,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="cold vs warm vs incremental build benchmark")
    parser.add_argument("--chains", type=int, default=4)
    parser.add_argument("--length", type=int, default=5)
    parser.add_argument("--work", type=int, default=15000, help="CPU iterations per target")
    parser.add_argument("--keep", action="store_true", help="keep the generated project")
    args = parser.parse_args(argv)

    root = Path(tempfile.mkdtemp(prefix="buildcache-bench-"))
    print(
        f"benchmark project: {root}\n"
        f"  shape: {args.chains} chains x {args.length} steps + 1 merge "
        f"= {args.chains * args.length + 1} targets, work={args.work}\n"
    )
    results = []
    try:
        targets = make_project(root, args.chains, args.length, args.work)

        # 1) cold build: empty cache
        engine = BuildEngine(targets, root)
        results.append(phase("cold build", engine))

        # 2) warm build: nothing changed
        engine = BuildEngine(targets, root)
        results.append(phase("warm build", engine))

        # 3) incremental: edit one seed file, rebuild
        seed = root / "src" / "chain1.txt"
        seed.write_text(seed.read_text(encoding="utf-8") + "one extra line\n", encoding="utf-8")
        engine = BuildEngine(targets, root)
        results.append(phase("incremental (1 edit)", engine))

        # 4) touch only the timestamp: content unchanged -> all cache hits
        seed.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        engine = BuildEngine(targets, root)
        results.append(phase("touch, same content", engine))

        cold, warm, inc = results[0], results[1], results[2]
        print("\nfindings:")
        if cold["wall"] > 0:
            print(f"  warm build is {cold['wall'] / max(warm['wall'], 1e-9):.1f}x faster than cold")
        print(f"  editing one seed re-executed {inc['executed']} of {inc['total']} targets")
        print(f"  timestamp-only change re-executed {results[3]['executed']} targets (content hashing)")
        print(f"  generated project: {root}" + ("" if args.keep else " (will be removed)"))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
