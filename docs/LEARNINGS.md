# Smart Build-Cache Invalidation — Step-by-Step Report and Learnings

**Assignment:** *Smart Build-Cache Invalidation*
**Deliverable:** a working incremental build engine in Python + this write-up.
**Repository:** <https://github.com/vishnuvardhank4126-lang/Smart-Build-Cache-Invalidation>

---

## 0. Summary

I built `buildcache`, a dependency-free incremental build engine that decides
what to rebuild by **hashing content**, not by comparing timestamps.

For each build step it computes an **action hash** from

> command + working directory + declared environment + **content hashes of the inputs** +
> **output digests of every dependency** + an environment fingerprint,

and it skips the step when that hash matches the previous run **and** the outputs
it produced are still byte-for-byte intact. The result is correct, fine-grained,
*transitive* invalidation with a human-readable reason for every rebuild.

Headline numbers (21-target synthetic project):

| Phase | Executed | Wall time |
| --- | --- | --- |
| Cold build | 21 / 21 | 26.66 s |
| Warm build | 0 / 21 | 0.20 s (**≈134× faster**) |
| Incremental (1 edit) | 6 / 21 | 5.92 s |
| `touch`, same content | 0 / 21 | 0.23 s |

58 unit tests pass.

---

## Step 1 — Understanding the problem

A *build cache* stores the outcome of a build step so it can be reused instead of
recomputed. The hard part is **invalidation**: deciding when a stored result is no
longer valid. Get it wrong in one direction and you ship stale artifacts; get it
wrong in the other and you rebuild everything and lose the benefit.

Classic `make` uses *timestamps*: "rebuild if any input is newer than the output".
This is simple but has three well-known failure modes:

1. **`touch` false positives** — rewriting a file with identical content still
   triggers a rebuild.
2. **Clock / checkout issues** — a fresh `git clone` gives every file a new
   timestamp, so everything rebuilds.
3. **It can't explain itself** — you get no idea *why* a step ran.

So "smart" invalidation must be based on **content**, must propagate through
dependencies, and should tell the user **why** something rebuilt. That became my
three concrete goals.

## Step 2 — Environment and setup

| Item | Value |
| --- | --- |
| OS | Windows 11 |
| Python | 3.14.6 |
| Git | 2.53.0 |
| Third-party libraries | **none** (standard library only) |

I deliberately kept the whole thing dependency-free (no PyYAML, no `networkx`,
no `pytest`) so a reviewer can clone and run it with a bare Python install. Two
consequences of that decision:

* the build graph is described in a **Python build file** instead of YAML, and
* the tests use the built-in `unittest` framework.

## Step 3 — Architecture

I split the engine into small, independently testable modules:

```
buildcache/
  model.py    Target, CacheRecord, Status, error types
  hashing.py  SHA-256 helpers
  graph.py    dependency graph + topological order
  cache.py    on-disk action cache
  config.py   loads the user's build.py
  engine.py   the invalidation decision + execution
  cli.py      build / explain / status / graph / clean
```

The data flow for one build is:

```
build.py ──load_targets──▶ [Target] ──▶ DependencyGraph ──topological_order──▶
   for each target:  hash inputs → collect dep digests → action hash →
   compare with CacheStore → HIT ? skip : execute → hash outputs → store record
```

Keeping hashing, graph and cache separate meant I could unit-test each piece in
isolation before wiring them together.

## Step 4 — Content hashing (`hashing.py`)

The foundation. Functions:

* `hash_file` — streams a file through SHA-256 in 1 MiB chunks (so huge files
  never load fully into memory).
* `hash_path` — hashes a file *or* a whole directory tree (sorted relative paths +
  contents), so directory inputs work too.
* `combine_hashes` — digest of a `{key: hash}` map, **sorted first** so the result
  is independent of iteration order.
* `environment_fingerprint` — SHA-256 over the Python version, implementation,
  platform and any declared extra environment variables.

Two design points I want to highlight:

* **Order independence.** `combine_hashes({"a":..., "b":...})` equals
  `combine_hashes({"b":..., "a":...})`. If it weren't, reordering inputs in the
  build file would silently invalidate the cache. This is asserted by a test.
* **Determinism.** Same bytes → same digest, always. A test pins the digest of
  `b"abc"` to the known SHA-256 value so a future refactor can't change the
  hashing scheme by accident.

## Step 5 — Dependency graph (`graph.py`)

`DependencyGraph` is built from the list of `Target`s and validates it eagerly:
duplicate names and dependencies on unknown targets raise immediately.

The ordering uses **Kahn's algorithm** with a heap, so ties are broken
alphabetically. That makes the build order *reproducible*, which matters for logs
and for benchmarking. `topological_order()` raises `CycleError` listing the
targets still stuck in a cycle, and there are tests for both a 2-node cycle and a
self-loop.

It also supports selective builds: `order_for(["report"])` returns only the
requested target **and its transitive dependencies**, in order.

## Step 6 — The action cache (`cache.py`)

The cache is one JSON file, `.buildcache/cache.json`, mapping a target name to a
`CacheRecord` (action hash, input hashes, dependency hashes, output hashes,
output digest, timings).

Two robustness decisions:

* **Atomic writes.** The file is written to a temp file and moved into place with
  `os.replace`, so an interrupted build can never leave a half-written cache.
* **Self-healing.** A corrupt or version-mismatched cache is moved aside
  (`cache.broken-<ts>`) with a warning and the build starts fresh. Failing a build
  because the *cache* is broken would be a bad trade — rebuilding is always safe.

## Step 7 — Loading the build file (`config.py`)

`load_targets` executes the user's `build.py` with `importlib` and reads either a
module-level `TARGETS` list or a `build_graph()` function. Authoring mistakes are
wrapped into a single `BuildError` with context, and every object is type-checked
to be a `Target`.

## Step 8 — The engine: the invalidation decision (`engine.py`)

This is the core. For each target, in dependency order:

1. **Hash the inputs.** Each declared input (or glob) is resolved relative to the
   target's `cwd` and content-hashed. A missing input is a hard error.
2. **Collect dependency digests.** The `output_digest` of every upstream target.
3. **Compute the action hash**:
   ```
   H( name · command · cwd · env · sorted(input hashes) · sorted(dep hashes) · env fingerprint )
   ```
4. **Decide.** It is a **cache hit** iff a record exists, its action hash matches,
   *and* `_outputs_valid` confirms every declared output still exists and hashes to
   its recorded value. Otherwise it is a **miss**.
5. **Execute and record.** Run the command, hash the produced outputs, store a new
   record. A declared output that was not produced is an error.

Three subtleties that make it "smart":

* **Transitive invalidation.** Because a dependency's `output_digest` is folded
  into the action hash, changing `a.txt` changes `a`'s output digest, which changes
  `b`'s action hash, and so on down the chain — no explicit propagation code needed.
* **Output verification.** Re-hashing outputs on every build means an externally
  modified or deleted output is detected and the step re-runs, rather than the
  stale file being trusted.
* **Phony targets.** A target with no command is a grouping node; its digest is
  derived from its dependencies so a change still flows through it (see Step 14).

`_explain` produces the reason string by *diffing* the new fingerprint against the
stored record: it reports the exact changed input, the changed dependency, a
command change, an environment change, or "output missing or modified externally".

## Step 9 — The CLI (`cli.py`)

```
buildcache build   [-p DIR] [--force] [--json] [TARGETS...]
buildcache explain [-p DIR] [TARGETS...]      # dry run, executes nothing
buildcache status  [-p DIR]                    # dump the cache
buildcache graph   [-p DIR] [--dot]            # build order / Graphviz
buildcache clean   [-p DIR] [--outputs]
```

`explain` reuses the same analysis code as `build` but stops before execution —
a genuinely useful feature: you can see the blast radius of an edit before paying
for it.

## Step 10 — Demo project (worked example)

`examples/demo_project` is a three-stage text pipeline:

```
data/docN.txt ─tokenize─▶ out/docN.tokens ─merge─▶ out/all.tokens ─count─▶ out/report.txt
```

First build (cold):

```
MISS    tokenize-doc1 (no cache entry yet (first build))
MISS    tokenize-doc2 (no cache entry yet (first build))
MISS    tokenize-doc3 (no cache entry yet (first build))
MISS    merge  (no cache entry yet (first build))
MISS    report (no cache entry yet (first build))
SKIPPED all (phony)
done: 0 hit, 5 rebuilt, 3.606s
```

Second build (nothing changed):

```
HIT     tokenize-doc1 (up to date)
HIT     tokenize-doc2 (up to date)
HIT     tokenize-doc3 (up to date)
HIT     merge  (up to date)
HIT     report (up to date)
done: 5 hit, 0 rebuilt, 0.657s
```

Now I edit **one** source file (`data/doc2.txt`) and ask `explain` first:

```
plan (no commands executed):
  would reuse    tokenize-doc1
  would rebuild  tokenize-doc2  <- input changed: data/doc2.txt
  would reuse    tokenize-doc3
  would rebuild  merge          <- dependency tokenize-doc2 changed
  would rebuild  report         <- dependency merge changed
  would skip     all
```

and build:

```
HIT     tokenize-doc1 (up to date)
MISS    tokenize-doc2 (input changed: data/doc2.txt)
HIT     tokenize-doc3 (up to date)
MISS    merge  (dependency tokenize-doc2 changed)
MISS    report (dependency merge changed)
done: 2 hit, 3 rebuilt, 1.525s
```

Exactly the desired behaviour: **only the affected chain re-runs.**

Finally I tampered with the artifacts directly — appended junk to
`out/doc1.tokens` and deleted `out/doc3.tokens` — and rebuilt:

```
MISS    tokenize-doc1 (output missing or modified externally)
HIT     tokenize-doc2 (up to date)
MISS    tokenize-doc3 (output missing or modified externally)
HIT     merge  (up to date)
HIT     report (up to date)
```

The engine refused to trust the tampered outputs.

## Step 11 — Tests

`python -m unittest discover -s tests -v` → **58 tests, all passing.**

Coverage by area:

* **hashing** — known digest, determinism, order-independence of `combine_hashes`,
  directory hashing, missing-file errors, environment fingerprint sensitivity.
* **graph** — dependency-before-dependent ordering, determinism, 2-node cycle,
  self-loop, unknown dependency, duplicate name, closure/`order_for`, DOT output.
* **cache** — CRUD, disk round-trip, corrupt-file quarantine, version mismatch,
  malformed-entry skipping, "no temp files left behind" atomicity check.
* **engine** — cold = all miss, warm = all hit, force, transitive invalidation,
  `touch`-only = hit, external output tamper detection, deleted output,
  command change, env change, phony propagation, selective build, preview
  (executes nothing), missing input, output-not-produced, failing command
  (raises and does **not** cache), corrupt cache tolerance.
* **cli** — `graph`, `--dot`, build → cached build, `--json`, `explain`, `status`,
  `clean --outputs`, and the error exit code for a missing project.

## Step 12 — Benchmark and results

`benchmark.py` generates a synthetic project (N independent chains × M steps + a
final merge) and measures four phases. Each worker burns a configurable number of
SHA-256 rounds so timings are meaningful.

**Light work (`--work 15000`), 21 targets:**

| Phase | Executed | Cached | Wall |
| --- | --- | --- | --- |
| Cold build | 21 | 0 | 11.24 s |
| Warm build | 0 | 21 | 0.21 s (54×) |
| Incremental (1 edit) | 6 | 15 | 2.58 s |
| `touch`, same content | 0 | 21 | 0.15 s |

**Heavy work (`--work 400000`), same 21 targets:**

| Phase | Executed | Cached | Wall |
| --- | --- | --- | --- |
| Cold build | 21 | 0 | 26.66 s |
| Warm build | 0 | 21 | 0.20 s (**134×**) |
| Incremental (1 edit) | 6 | 15 | 5.92 s |
| `touch`, same content | 0 | 21 | 0.23 s |

Observations:

* The warm build is ~0.2 s regardless of how much work each step does, because it
  neither spawns the 21 child processes nor performs their computation. The
  speed-up therefore combines **avoiding work** with **avoiding process startup**
  — I ran the heavy-work variant specifically to confirm the benefit is not *only*
  interpreter startup.
* Editing a single seed re-executed exactly **6 of 21** targets: the 5 steps of the
  affected chain plus the merge. The other 15 were untouched.
* A `touch` with identical bytes re-executed **0** targets — the timestamp
  false-positive of `make` is gone.

## Step 13 — Design trade-offs

* **Hash outputs on every build.** Correctness (detecting tampering/staleness) vs.
  a little extra I/O. For big artifacts a real system would use mtimes+sizes as a
  fast path; I chose the safe default and documented it.
* **Environment fingerprint includes the interpreter/platform.** Moving to a new
  Python or OS invalidates the whole cache. Safe, but conservative; a production
  tool would let you pin which variables matter.
* **Cache keyed by target name, not purely by action hash.** Storing the *last*
  record per target is what makes `explain` possible (I can diff new vs. old).
  A content-addressable store keyed only by action hash would allow sharing results
  between machines but could not explain changes as easily.
* **No parallelism.** Steps run sequentially. The graph already gives the required
  ordering, so parallel execution is the most natural next step (see below).
* **Single JSON cache file.** Simple and inspectable; a very large graph would
  prefer a directory of per-target records.

## Step 14 — Problems I hit and how I fixed them

1. **Phony-target digest bug (caught during review).** My first version gave a
   phony grouping target a constant digest. That meant a target that depended
   *only* on a phony group would never invalidate when the group's real
   dependencies changed — a silent correctness bug. **Fix:** derive a phony
   target's digest from its dependencies' digests, and added
   `test_phony_target_skips_and_propagates_dependency_digest` to lock it in.
2. **A stale cache must never be fatal.** My first instinct was to raise on a
   corrupt cache file. That would break builds for a reason unrelated to their
   inputs. **Fix:** quarantine the broken file and continue with an empty cache,
   plus a warning; tested.
3. **Two of my own tests were wrong, not the engine.** The CLI test's helper script
   didn't consume its `mode` argument, so it raised `CommandError` (a `BuildError`
   subclass) and the CLI correctly returned exit code 2; and a "command change"
   test appended an argument the helper couldn't parse. **Fix:** corrected the
   fixtures. The lesson: when a test fails, check whether the *test* is wrong
   before changing the code under test.
4. **Terminal noise.** Every PowerShell command printed an execution-policy error
   about a profile snapshot. It was harmless (unrelated to the project) and did not
   affect exit codes, so I filtered it rather than "fixing" something I don't own.

## Step 15 — Key learnings

* **Content hashing beats timestamps.** It removes `touch` false positives, is
  robust to fresh checkouts and clock skew, and composes neatly: hashing the
  *hashes* lets a change propagate transitively with almost no extra code.
* **Order independence is essential.** `combine_hashes` sorts before hashing.
  Without that, harmless reordering would poison the cache.
* **Fail safe, not fatal.** Cache corruption should degrade to "rebuild", never to
  "can't build".
* **Explainability is a feature.** `explain` (what *would* rebuild and why) was
  cheap to build because the decision logic already computes the reason, and it
  makes the cache trustworthy.
* **Measure to understand.** The first benchmark looked like a 54× speedup; the
  heavy-work run (134×) showed that most of the light-work gain came from skipping
  process startup. Running the second configuration changed how I described the
  result — a good reminder to design benchmarks that isolate what you think you
  are measuring.
* **Small modules pay off.** Because `hashing`, `graph`, and `cache` were separate,
  each got its own focused test file and bugs were localized.

## Step 16 — How to reproduce

```bash
git clone https://github.com/vishnuvardhank4126-lang/Smart-Build-Cache-Invalidation
cd Smart-Build-Cache-Invalidation

# demo
python -m buildcache build   -p examples/demo_project
python -m buildcache build   -p examples/demo_project        # all cache hits
python -m buildcache explain -p examples/demo_project
python -m buildcache status  -p examples/demo_project

# tests
python -m unittest discover -s tests -v

# benchmark
python benchmark.py --chains 4 --length 5 --work 400000
```

## Step 17 — Possible next steps

* Parallel execution of independent targets (`jobs` pool driven by the graph).
* A content-addressable output store so identical outputs are de-duplicated and
  the cache can be shared between machines.
* Watching the filesystem for changes to drive a continuous incremental loop.
* Remote cache backend (keyed by action hash) for CI fan-out.
