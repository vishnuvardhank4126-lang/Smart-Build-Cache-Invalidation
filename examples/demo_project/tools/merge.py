"""Stage 2 worker: concatenate several token files into one."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: merge.py <in1> <in2> ... <out>", file=sys.stderr)
        return 2
    sources = [Path(p) for p in sys.argv[1:-1]]
    dst = Path(sys.argv[-1])

    lines: list[str] = []
    for source in sources:
        lines.extend(source.read_text(encoding="utf-8").splitlines())

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"merge: {len(sources)} file(s) -> {dst} ({len(lines)} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
