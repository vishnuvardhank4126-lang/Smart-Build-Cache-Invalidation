# Smart Build-Cache Invalidation

A small, dependency-free **content-addressed incremental build engine** for Python.
It rebuilds only the work that actually changed: instead of trusting file
timestamps (the classic `make` rule), it hashes the *contents* of every input,
the command, the upstream results and the environment, and skips a step whenever
that fingerprint is unchanged **and** its previous outputs are still intact.

```
targets:  a.txt ──tokenize──▶ out/a.tokens ──merge──▶ out/all.tokens ──report──▶ out/report.txt
```

Edit one source file → only the affected chain re-runs; everything else is a
cache hit.

---

## Why it is "smart"

| Classic Make-style rule | This engine |
| --- | --- |
| Rebuild if an input is *newer* than its output | Rebuild if input **content hash** changed |
| `touch` invalidates everything | `touch` invalidates nothing (bytes unchanged) |
| Can't tell *why* something rebuilt | Reports the exact reason (`input changed: data/doc2.txt`) |
| Trusts existing outputs | Re-hashes outputs; external tampering re-triggers the step |
| Ignores upstream results | Folds each dependency's output digest into the action hash → transitive invalidation |
| Ignores environment | Interpreter/platform/declared env vars are part of the fingerprint |

## Quick start

```bash
# no installation needed - it is pure standard library
python -m buildcache build   -p examples/demo_project   # first build: everything runs
python -m buildcache build   -p examples/demo_project   # second build: all cache hits
python -m buildcache explain -p examples/demo_project    # what *would* rebuild, and why
python -m buildcache status  -p examples/demo_project    # inspect the cache
python -m buildcache graph   -p examples/demo_project    # print the build order
python -m buildcache clean   -p examples/demo_project --outputs
```

Run the tests and the benchmark:

```bash
python -m unittest discover -s tests -v
python benchmark.py --chains 4 --length 5 --work 400000
```

## Declaring a build graph

A build file is an ordinary Python module (`build.py`) exposing a `TARGETS` list:

```python
from buildcache import Target

TARGETS = [
    Target(
        name="tokenize-doc1",
        command=["python", "tools/tokenize.py", "data/doc1.txt", "out/doc1.tokens"],
        inputs=["data/doc1.txt", "tools/tokenize.py"],   # content-hashed
        outputs=["out/doc1.tokens"],                      # verified after each run
    ),
    Target(
        name="merge",
        command=["python", "tools/merge.py", "out/doc1.tokens", "out/merge.tokens"],
        inputs=["tools/merge.py"],
        outputs=["out/merge.tokens"],
        deps=["tokenize-doc1"],                           # transitive invalidation
    ),
]
```

## How a single build decision is made

For every target, in dependency order:

1. hash the contents of each declared input file;
2. take the output digest of each dependency;
3. compute the **action hash** = `H(command · cwd · env · input hashes · dep hashes · env fingerprint)`;
4. **cache hit** if a record exists whose action hash matches *and* every declared
   output still hashes to its recorded value → execute nothing;
5. otherwise run the command, hash the outputs, and store a new record.

## Results

Synthetic project: 4 chains × 5 steps + 1 merge = **21 targets**.
Full output is in [docs/LEARNINGS.md](docs/LEARNINGS.md).

| Phase | Targets executed | Wall time |
| --- | --- | --- |
| Cold build (empty cache) | 21 / 21 | 26.66 s |
| Warm build (nothing changed) | 0 / 21 | 0.20 s (**≈134× faster**) |
| Incremental (edit one seed file) | 6 / 21 | 5.92 s |
| `touch` only (same bytes) | 0 / 21 | 0.23 s |

All 58 unit tests pass: `python -m unittest discover -s tests`.

## Repository layout

```
buildcache/          the engine package
  model.py           Target, CacheRecord, Status, errors
  hashing.py         SHA-256 helpers (files, dirs, ordered/unordered sets)
  graph.py           dependency graph + deterministic topological order
  cache.py           atomic, self-healing JSON action cache
  config.py          loads the user build file
  engine.py          invalidation decision + execution
  cli.py             build / explain / status / graph / clean
examples/demo_project/   runnable 3-stage text pipeline
tests/               58 unit tests
benchmark.py         cold vs warm vs incremental benchmark
docs/LEARNINGS.md    step-by-step write-up of the assignment
```

## License

MIT
