"""Verify CLI output writes preserve stable errors during failed cleanup."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kuma.cli import _emit_or_save
from kuma.exceptions import KumaError


class CliAtomicOutputTests(unittest.TestCase):
    def test_writes_complete_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            _emit_or_save({"status": "ok"}, str(output))

            self.assertEqual(json.loads(output.read_text()), {"status": "ok"})

    def test_replace_failure_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            output.write_text('"previous"', encoding="utf-8")
            with (
                patch("kuma.cli.os.replace", side_effect=OSError("replace failed")),
                self.assertRaises(KumaError) as raised,
            ):
                _emit_or_save({}, str(output))

            self.assertEqual(raised.exception.code, "strategy_scan_invalid")
            self.assertEqual(tuple(Path(directory).iterdir()), (output,))
            self.assertEqual(output.read_text(encoding="utf-8"), '"previous"')

    def test_cleanup_failure_does_not_mask_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            with (
                patch("kuma.cli.os.replace", side_effect=OSError("replace failed")),
                patch.object(
                    Path, "unlink", side_effect=PermissionError("cleanup failed")
                ),
                self.assertRaises(KumaError) as raised,
            ):
                _emit_or_save({}, str(output))

            self.assertEqual(raised.exception.code, "strategy_scan_invalid")
            self.assertEqual(
                str(raised.exception), "Strategy Group output could not be saved"
            )


if __name__ == "__main__":
    unittest.main()
