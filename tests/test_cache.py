"""Tests for buildcache.cache."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildcache.cache import CACHE_VERSION, CacheStore
from buildcache.model import CacheRecord


def make_record(name: str, action_hash: str = "act", output_digest: str = "out") -> CacheRecord:
    return CacheRecord(
        name=name,
        action_hash=action_hash,
        status="MISS",
        command="argv:tool",
        input_hashes={"in.txt": "h1"},
        dep_hashes={},
        output_hashes={"out.txt": output_digest},
        output_digest=output_digest,
        env_fingerprint="fp",
        timestamp=1.0,
        duration=0.25,
    )


class CacheStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".buildcache"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_get_put_len_clear(self) -> None:
        store = CacheStore(self.root).load()
        self.assertEqual(len(store), 0)
        store.put(make_record("a"))
        self.assertIn("a", store)
        self.assertEqual(store.get("a").action_hash, "act")
        self.assertEqual(store.clear(), 1)
        self.assertIsNone(store.get("a"))

    def test_round_trip_through_disk(self) -> None:
        store = CacheStore(self.root).load()
        store.put(make_record("a", action_hash="aaa", output_digest="ooo"))
        store.save()

        reloaded = CacheStore(self.root).load()
        record = reloaded.get("a")
        self.assertIsNotNone(record)
        self.assertEqual(record.action_hash, "aaa")
        self.assertEqual(record.output_digest, "ooo")
        self.assertEqual(record.output_hashes, {"out.txt": "ooo"})

    def test_missing_file_is_empty_cache(self) -> None:
        store = CacheStore(self.root).load()
        self.assertEqual(len(store), 0)
        self.assertEqual(store.warnings, [])

    def test_corrupt_cache_is_quarantined_not_fatal(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cache.json").write_text("{ this is not json", encoding="utf-8")
        store = CacheStore(self.root).load()
        self.assertEqual(len(store), 0)
        self.assertTrue(any("unreadable" in w for w in store.warnings))
        # the broken file was moved aside, not deleted
        self.assertTrue(list(self.root.glob("cache.broken-*")))

    def test_version_mismatch_starts_fresh(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cache.json").write_text(
            json.dumps({"version": CACHE_VERSION + 99, "targets": {"a": {}}}),
            encoding="utf-8",
        )
        store = CacheStore(self.root).load()
        self.assertEqual(len(store), 0)

    def test_malformed_entry_is_skipped(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "cache.json").write_text(
            json.dumps({"version": CACHE_VERSION, "targets": {"good": make_record("good").to_dict(), "bad": {"unexpected": 1}}}),
            encoding="utf-8",
        )
        store = CacheStore(self.root).load()
        self.assertIsNotNone(store.get("good"))

    def test_save_is_atomic_no_tmp_left(self) -> None:
        store = CacheStore(self.root).load()
        store.put(make_record("a"))
        store.save()
        leftovers = list(self.root.glob(".cache-*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
