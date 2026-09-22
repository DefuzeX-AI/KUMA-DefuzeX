"""Verify the minimal Agent example offline, including failure and cleanup paths."""

from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from kuma.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "minimal_local", ROOT / "examples/minimal_local.py"
)
assert SPEC is not None and SPEC.loader is not None
EXAMPLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXAMPLE)


class MinimalLocalTests(unittest.TestCase):
    def test_success(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(EXAMPLE.main(), 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "input=Return a bounded maintenance result.",
                "state=completed",
                "submissions=1",
                "submission_status=completed",
                "report=None",
            ],
        )

    def test_agent_failures_are_not_success(self) -> None:
        for error, status in (
            (RuntimeError("secret-token"), "failed"),
            (TimeoutError("secret-token"), "timeout"),
        ):
            with self.subTest(status=status):
                output = io.StringIO()
                with (
                    patch.object(EXAMPLE, "call_your_agent", side_effect=error),
                    redirect_stdout(output),
                ):
                    self.assertEqual(EXAMPLE.main(), 1)
                self.assertIn(f"submission_status={status}", output.getvalue())
                self.assertIn("report=None", output.getvalue())
                self.assertNotIn("secret-token", output.getvalue())

    def test_invalid_output_releases_runtime(self) -> None:
        with (
            patch.object(EXAMPLE, "call_your_agent", return_value=None),
            self.assertRaises(ValidationError),
        ):
            EXAMPLE.main()
        # A leaked active-Run lease would prevent the next Run from starting.
        self.test_success()

    def test_interruption_releases_runtime(self) -> None:
        with (
            patch.object(EXAMPLE, "call_your_agent", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            EXAMPLE.main()
        self.test_success()


if __name__ == "__main__":
    unittest.main()
