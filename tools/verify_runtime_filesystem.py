"""Verify repository runtime storage rejects redirected directories."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from kuma.errors import ConfigurationError
from kuma.runtime import ensure_repo_runtime_directory


class RuntimeFilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_and_reuse_runtime_directory(self) -> None:
        first = ensure_repo_runtime_directory(self.root)
        second = ensure_repo_runtime_directory(self.root)

        self.assertEqual(first, second)
        self.assertTrue(first.is_dir())
        self.assertFalse(first.is_symlink())
        self.assertEqual(
            (self.root / ".gitignore").read_text(encoding="utf-8").count("/.kuma/"),
            1,
        )

    def test_rejects_symlinked_runtime_directory(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.root / ".kuma", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        with self.assertRaisesRegex(ConfigurationError, "storage is unavailable"):
            ensure_repo_runtime_directory(self.root)

    def test_rejects_regular_file_collision(self) -> None:
        (self.root / ".kuma").touch()

        with self.assertRaisesRegex(ConfigurationError, "storage is unavailable"):
            ensure_repo_runtime_directory(self.root)


if __name__ == "__main__":
    unittest.main()
