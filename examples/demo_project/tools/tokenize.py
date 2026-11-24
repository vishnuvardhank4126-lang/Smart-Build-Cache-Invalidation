"""Stage 1 worker: split a text file into whitespace-separated tokens."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: tokenize.py <input.txt> <output.tokens>", file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    tokens = src.read_text(encoding="utf-8").split()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(tokens) + "\n", encoding="utf-8")
    print(f"tokenize: {src} -> {dst} ({len(tokens)} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
