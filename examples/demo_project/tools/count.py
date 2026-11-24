"""Stage 3 worker: count tokens and write a JSON report."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: count.py <input.tokens> <report.json>", file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    tokens = src.read_text(encoding="utf-8").split()
    counter = collections.Counter(token.lower() for token in tokens)

    report = {
        "total_tokens": len(tokens),
        "unique_tokens": len(counter),
        "top_5": counter.most_common(5),
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report: {len(tokens)} tokens, {len(counter)} unique -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
