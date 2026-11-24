"""Smart Build-Cache Invalidation.

A small, dependency-free, content-addressed incremental build engine.

The engine decides whether a build step has to run again by hashing everything
that can affect its result:

* the command itself,
* the *contents* of the declared input files,
* the result digests of the upstream targets it depends on,
* an environment fingerprint (interpreter / platform / declared env vars).

That combination is the **action hash**.  If the action hash is unchanged *and*
the outputs produced by the previous run are still byte-for-byte identical, the
step is skipped.  This gives correct, fine-grained invalidation instead of the
blunt "timestamp is newer" rule used by classic Make.

Public API
----------
``Target`` / ``buildcache.dsl``  describe a build graph.
``BuildEngine``                  runs it.
``CacheStore``                   the on-disk action cache.
"""

from .model import (
    BuildError,
    CacheRecord,
    CommandError,
    CycleError,
    GraphError,
    Status,
    Target,
)
from .hashing import (
    combine_hashes,
    environment_fingerprint,
    hash_bytes,
    hash_file,
    hash_path,
    hash_string,
)
from .graph import DependencyGraph
from .cache import CacheStore
from .config import find_build_file, load_targets
from .engine import BuildEngine, BuildReport, BuildResult

__version__ = "1.0.0"

__all__ = [
    "BuildError",
    "CacheRecord",
    "CommandError",
    "CycleError",
    "GraphError",
    "Status",
    "Target",
    "combine_hashes",
    "environment_fingerprint",
    "hash_bytes",
    "hash_file",
    "hash_path",
    "hash_string",
    "DependencyGraph",
    "CacheStore",
    "find_build_file",
    "load_targets",
    "BuildEngine",
    "BuildReport",
    "BuildResult",
    "__version__",
]
