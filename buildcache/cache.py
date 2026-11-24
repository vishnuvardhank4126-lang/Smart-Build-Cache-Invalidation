"""On-disk action cache.

The cache is a single JSON document (``.buildcache/cache.json``) mapping a target
name to its :class:`~buildcache.model.CacheRecord`.  Writes are atomic
(temp file + ``os.replace``) so an interrupted build can never leave a
half-written, unreadable cache behind.

A corrupt cache is *not* fatal: it is moved aside, a warning is recorded, and the
build starts from an empty cache.  Failing the build because of a broken cache
file would be a bad trade-off - rebuilding is always safe.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union

from .model import CacheRecord

__all__ = ["CacheStore", "CACHE_FILENAME", "CACHE_VERSION"]

CACHE_FILENAME = "cache.json"
CACHE_VERSION = 1


class CacheStore:
    """Load / query / persist the action cache for one project."""

    def __init__(self, root: Union[str, os.PathLike]) -> None:
        self.root = Path(root)
        self.path = self.root / CACHE_FILENAME
        self.warnings: List[str] = []
        self._records: Dict[str, CacheRecord] = {}
        self._loaded = False

    # -- persistence -------------------------------------------------------- #
    def load(self) -> "CacheStore":
        """Read the cache from disk (idempotent)."""
        if self._loaded:
            return self
        self._loaded = True
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._quarantine(f"cache file is unreadable ({exc}); starting fresh")
            return self

        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            self._quarantine("cache format changed; starting fresh")
            return self

        for name, payload in (raw.get("targets") or {}).items():
            try:
                self._records[name] = CacheRecord.from_dict(payload)
            except (TypeError, ValueError):
                self.warnings.append(f"ignoring malformed cache entry for {name!r}")
        return self

    def save(self) -> None:
        """Atomically write the cache back to disk."""
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CACHE_VERSION,
            "saved_at": time.time(),
            "targets": {name: rec.to_dict() for name, rec in sorted(self._records.items())},
        }
        data = json.dumps(payload, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.root), prefix=".cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def _quarantine(self, message: str) -> None:
        broken = self.path.with_suffix(f".broken-{int(time.time())}")
        try:
            os.replace(self.path, broken)
            message += f" (previous file kept at {broken.name})"
        except OSError:
            pass
        self.warnings.append(message)

    # -- access ------------------------------------------------------------- #
    def get(self, name: str) -> Optional[CacheRecord]:
        return self._records.get(name)

    def put(self, record: CacheRecord) -> None:
        self._records[record.name] = record

    def drop(self, name: str) -> bool:
        return self._records.pop(name, None) is not None

    def clear(self) -> int:
        """Forget every entry (does not touch the filesystem)."""
        count = len(self._records)
        self._records.clear()
        return count

    def records(self) -> Dict[str, CacheRecord]:
        return dict(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, name: object) -> bool:
        return name in self._records

    def __iter__(self) -> Iterator[CacheRecord]:
        return iter(self._records.values())
