"""Tests for buildcache.hashing."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from buildcache.hashing import (
    combine_hashes,
    environment_fingerprint,
    hash_bytes,
    hash_file,
    hash_path,
    hash_string,
    hash_strings,
)
from buildcache.model import BuildError


class HashBytesTests(unittest.TestCase):
    def test_known_sha256(self) -> None:
        # sha256("abc")
        self.assertEqual(
            hash_bytes(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_deterministic(self) -> None:
        self.assertEqual(hash_bytes(b"payload"), hash_bytes(b"payload"))

    def test_hash_string_matches_bytes(self) -> None:
        self.assertEqual(hash_string("hello"), hash_bytes(b"hello"))

    def test_hash_strings_is_order_sensitive(self) -> None:
        self.assertNotEqual(hash_strings(["a", "b"]), hash_strings(["b", "a"]))


class HashFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_file_matches_bytes(self) -> None:
        path = self.root / "a.bin"
        path.write_bytes(b"content-123")
        self.assertEqual(hash_file(path), hash_bytes(b"content-123"))

    def test_content_change_changes_hash(self) -> None:
        path = self.root / "a.bin"
        path.write_bytes(b"one")
        first = hash_file(path)
        path.write_bytes(b"two")
        self.assertNotEqual(first, hash_file(path))

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(BuildError):
            hash_file(self.root / "nope.bin")


class HashPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_directory_hash_is_content_addressed(self) -> None:
        tree = self.root / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "a.txt").write_text("a", encoding="utf-8")
        (tree / "sub" / "b.txt").write_text("b", encoding="utf-8")
        first = hash_path(tree)

        # Same content, new mtime -> identical digest.
        (tree / "a.txt").write_text("a", encoding="utf-8")
        self.assertEqual(first, hash_path(tree))

        # Content change -> different digest.
        (tree / "sub" / "b.txt").write_text("B", encoding="utf-8")
        self.assertNotEqual(first, hash_path(tree))

    def test_missing_path_raises(self) -> None:
        with self.assertRaises(BuildError):
            hash_path(self.root / "ghost")


class CombineHashesTests(unittest.TestCase):
    def test_order_independent(self) -> None:
        left = {"a": hash_string("1"), "b": hash_string("2")}
        right = {"b": hash_string("2"), "a": hash_string("1")}
        self.assertEqual(combine_hashes(left), combine_hashes(right))

    def test_value_change_changes_digest(self) -> None:
        self.assertNotEqual(
            combine_hashes({"a": hash_string("1")}),
            combine_hashes({"a": hash_string("2")}),
        )

    def test_empty_is_stable(self) -> None:
        self.assertEqual(combine_hashes({}), combine_hashes({}))


class EnvironmentFingerprintTests(unittest.TestCase):
    def test_stable_for_same_input(self) -> None:
        self.assertEqual(environment_fingerprint({"A": "1"}), environment_fingerprint({"A": "1"}))

    def test_changes_with_env(self) -> None:
        self.assertNotEqual(
            environment_fingerprint({"A": "1"}),
            environment_fingerprint({"A": "2"}),
        )


if __name__ == "__main__":
    unittest.main()
