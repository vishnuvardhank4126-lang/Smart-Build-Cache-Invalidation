"""Deterministic hashing helpers.

Everything in the cache is keyed by SHA-256 digests.  Two rules matter for
correctness:

* hashing must be **deterministic** (same bytes -> same digest), and
* hashing a *collection* must be **order independent**, so a target can list its
  inputs in any order and still hit the same cache entry.  :func:`combine_hashes`
  sorts its items before hashing, which provides that guarantee.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from typing import Iterable, Mapping, Optional

from .model import BuildError

__all__ = [
    "hash_bytes",
    "hash_string",
    "hash_file",
    "hash_path",
    "combine_hashes",
    "hash_strings",
    "environment_fingerprint",
]

_CHUNK_SIZE = 1 << 20  # 1 MiB


def hash_bytes(data: bytes) -> str:
    """SHA-256 of raw bytes, hex encoded."""
    return hashlib.sha256(data).hexdigest()


def hash_string(text: str) -> str:
    """SHA-256 of a UTF-8 string."""
    return hash_bytes(text.encode("utf-8"))


def hash_file(path: os.PathLike | str) -> str:
    """Stream a file through SHA-256 without loading it fully into memory."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:  # unreadable / disappeared mid-build
        raise BuildError(f"cannot read {os.fspath(path)!r}: {exc}") from exc
    return digest.hexdigest()


def hash_path(path: os.PathLike | str) -> str:
    """Hash a file, or a whole directory tree (sorted paths + contents)."""
    path = os.fspath(path)
    if os.path.isdir(path):
        entries = {}
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for filename in sorted(files):
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                entries[rel] = hash_file(full)
        if not entries:  # empty directory still needs a stable identity
            return hash_string("<empty-dir>" + os.path.basename(os.path.abspath(path)))
        return combine_hashes(entries)
    if os.path.isfile(path):
        return hash_file(path)
    raise BuildError(f"path does not exist: {path!r}")


def combine_hashes(items: Mapping[str, str]) -> str:
    """Order-independent digest of a ``{key: hash}`` mapping."""
    payload = json.dumps(sorted(items.items()), ensure_ascii=True, separators=(",", ":"))
    return hash_string(payload)


def hash_strings(values: Iterable[str]) -> str:
    """Digest of an ordered sequence of strings."""
    return hash_string("\x1f".join(values))


def environment_fingerprint(extra: Optional[Mapping[str, str]] = None) -> str:
    """Fingerprint of the parts of the environment a command may depend on.

    Interpreter version and platform are included so that upgrading Python (or
    moving to another OS) invalidates the whole cache, which is the safe default.
    """
    data = {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "machine": platform.machine(),
    }
    if extra:
        data.update({f"env:{key}": str(value) for key, value in sorted(extra.items())})
    return combine_hashes(data)
