"""Verify the single-agent template offline, including cleanup and failure paths."""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kuma import ConfigurationError, ValidationError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "single_agent_template", ROOT / "examples/single_agent_template/app.py"
)
assert SPEC is not None and SPEC.loader is not None
EXAMPLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXAMPLE)

_DISABLED_ENVIRONMENT = (
    "KUMA_API_KEY",
    "KUMA_REPO_PATH",
    "KUMA_USE_OFFICIAL",
    "KUMA_ALLOW_LOCAL",
    "KUMA_AGENT_PROFILE_PATH",
)


class SingleAgentTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "KUMA_DISABLE_UPDATE_CHECK": "1",
                "KUMA_AGENT_TIMEOUT_SECONDS": "30",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in _DISABLED_ENVIRONMENT:
            os.environ.pop(name, None)
        network = patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network call"),
        )
        network.start()
        self.addCleanup(network.stop)
        self.runs: list[Any] = []
        original_create_run = EXAMPLE.create_run

        def create_run(**kwargs: Any) -> Any:
            run = original_create_run(**kwargs)
            self.runs.append(run)
            return run

        creator = patch.object(EXAMPLE, "create_run", side_effect=create_run)
        creator.start()
        self.addCleanup(creator.stop)
        self.addCleanup(self._cancel_unfinished_runs)

    def _cancel_unfinished_runs(self) -> None:
        for run in self.runs:
            if run.state in {"ready", "input_delivered"}:
                run.cancel()

    def _run_main(self, argv: list[str] | None = None) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = EXAMPLE.main([] if argv is None else argv)
        return status, output.getvalue(), errors.getvalue()

    def _assert_success(self) -> None:
        status, output, errors = self._run_main()
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertIn("run_state=completed\n", output)
        self.assertIn("history_items=1\n", output)
        self.assertIn("last_submission_status=completed\n", output)
        self.assertIn("report=None\n", output)
        self.assertEqual(self.runs[-1].state, "completed")

    def test_success_is_local_and_complete(self) -> None:
        self._assert_success()

    def test_smoke_failure_modes_are_truthful(self) -> None:
        cases = (
            ("agent", "failed", "agent_failed"),
            ("timeout", "timeout", "agent_timeout"),
            ("empty", "failed", "agent_empty_result"),
            ("non-json", "failed", "agent_result_not_json"),
        )
        for mode, status, error in cases:
            with self.subTest(mode=mode):
                code, output, errors = self._run_main(["--smoke-failure", mode])
                self.assertEqual(code, 1)
                self.assertIn(f"last_submission_status={status}\n", output)
                self.assertIn(f"template_error={error}\n", errors)
                self._assert_success()

    def test_agent_exception_is_failed_without_leaking_text(self) -> None:
        with patch.object(
            EXAMPLE,
            "call_your_agent",
            side_effect=RuntimeError("private-agent-detail"),
        ):
            status, output, errors = self._run_main()
        self.assertEqual(status, 1)
        self.assertIn("last_submission_status=failed\n", output)
        self.assertIn("template_error=agent_failed\n", errors)
        self.assertNotIn("private-agent-detail", output + errors)
        self._assert_success()

    def test_rejected_submission_cancels_unfinished_run(self) -> None:
        with (
            redirect_stdout(io.StringIO()),
            patch.object(EXAMPLE, "_validated_result", return_value=object()),
            self.assertRaises(ValidationError),
        ):
            EXAMPLE.main([])
        self.assertEqual(self.runs[-1].state, "cancelled")
        self._assert_success()

    def test_interruption_cancels_unfinished_run(self) -> None:
        with (
            redirect_stdout(io.StringIO()),
            patch.object(EXAMPLE, "call_your_agent", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            EXAMPLE.main([])
        self.assertEqual(self.runs[-1].state, "cancelled")
        self._assert_success()

    def test_failure_before_input_cancels_ready_run(self) -> None:
        with (
            redirect_stdout(io.StringIO()),
            patch.object(EXAMPLE, "_run_inputs", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            EXAMPLE.main([])
        self.assertEqual(self.runs[-1].state, "cancelled")
        self._assert_success()

    def test_invalid_timeout_creates_no_run(self) -> None:
        with (
            patch.dict(os.environ, {"KUMA_AGENT_TIMEOUT_SECONDS": "invalid"}),
            self.assertRaises(ConfigurationError),
        ):
            EXAMPLE.main([])
        self.assertEqual(self.runs, [])

    def test_official_mode_requires_repository_path(self) -> None:
        with (
            patch.dict(os.environ, {"KUMA_USE_OFFICIAL": "1"}),
            self.assertRaises(ConfigurationError),
        ):
            EXAMPLE.main([])
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
