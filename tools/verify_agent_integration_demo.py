"""Verify the Agent integration demo's lifecycle without hosted or model calls."""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from kuma import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agent_integration_demo", ROOT / "examples/agent_integration_demo/app.py"
)
assert SPEC is not None and SPEC.loader is not None
EXAMPLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXAMPLE)


class AgentIntegrationDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "KUMA_API_KEY": "",
                "KUMA_REPO_PATH": "",
                "KUMA_DISABLE_UPDATE_CHECK": "1",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        network = patch(
            "socket.socket.connect",
            side_effect=AssertionError("Unexpected network call"),
        )
        network.start()
        self.addCleanup(network.stop)
        self.runs = []
        original_create_run = EXAMPLE.create_run

        def create_run(**kwargs):
            run = original_create_run(**kwargs)
            self.runs.append(run)
            return run

        creator = patch.object(EXAMPLE, "create_run", side_effect=create_run)
        creator.start()
        self.addCleanup(creator.stop)
        self.addCleanup(self.cancel_unfinished_runs)

    def cancel_unfinished_runs(self) -> None:
        for run in self.runs:
            if run.state in {"ready", "input_delivered"}:
                run.cancel()

    def run_local(self) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            status = EXAMPLE.run_demo(hosted=False)
        return status, output.getvalue()

    def test_success_without_credentials_or_network(self) -> None:
        status, output = self.run_local()
        self.assertEqual(status, 0)
        self.assertIn("mode=mock\n", output)
        self.assertIn("cost=free_local_no_api_call\n", output)
        self.assertIn("run_state=completed\n", output)
        self.assertIn("history_items=1\n", output)
        self.assertIn("submission_status=completed\n", output)
        self.assertIn("Mock Agent completed successfully.", output)

    def test_agent_failures_are_recorded_without_exception_text(self) -> None:
        for error, expected in (
            (RuntimeError("private-agent-detail"), "failed"),
            (TimeoutError("private-agent-detail"), "timeout"),
        ):
            with self.subTest(expected=expected):
                with patch.object(EXAMPLE, "call_your_agent", side_effect=error):
                    status, output = self.run_local()
                self.assertEqual(status, 1)
                self.assertIn(f"submission_status={expected}\n", output)
                self.assertNotIn("private-agent-detail", output)
                submission = self.runs[-1].history[-1].submission
                self.assertEqual(submission.status, expected)
                self.assertEqual(
                    submission.error,
                    "Agent timed out" if expected == "timeout" else "Agent failed",
                )
                self.assertIsNone(submission.output)
                self.test_success_without_credentials_or_network()

    def test_invalid_output_releases_runtime(self) -> None:
        with (
            redirect_stdout(io.StringIO()),
            patch.object(EXAMPLE, "call_your_agent", return_value=None),
            self.assertRaises(ValidationError),
        ):
            EXAMPLE.run_demo(hosted=False)
        self.assertEqual(self.runs[-1].state, "cancelled")
        # The next Run fails if the active runtime lease was left behind.
        self.test_success_without_credentials_or_network()

    def test_interruption_releases_runtime(self) -> None:
        with (
            redirect_stdout(io.StringIO()),
            patch.object(EXAMPLE, "call_your_agent", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            EXAMPLE.run_demo(hosted=False)
        self.assertEqual(self.runs[-1].state, "cancelled")
        self.test_success_without_credentials_or_network()

    def test_no_input_is_not_success_and_cancels_unfinished_run(self) -> None:
        run = Mock(state="ready")
        run.get_input.return_value = None
        with redirect_stdout(io.StringIO()):
            self.assertEqual(EXAMPLE._complete_run(run), 1)
        run.submit.assert_not_called()
        run.cancel.assert_called_once_with()

    def test_submit_error_is_not_relabelled_as_an_agent_failure(self) -> None:
        run = Mock(state="input_delivered")
        run.get_input.return_value = "Example input"
        error = ValidationError("Invalid Agent output", code="output_invalid")
        run.submit.side_effect = error
        with self.assertRaises(ValidationError) as raised:
            EXAMPLE._complete_run(run)
        self.assertIs(raised.exception, error)
        run.submit.assert_called_once()
        run.cancel.assert_called_once_with()

    def test_main_propagates_agent_failure_exit_status(self) -> None:
        with (
            patch("sys.argv", ["app.py"]),
            patch.object(EXAMPLE, "call_your_agent", side_effect=RuntimeError),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(EXAMPLE.main(), 1)

    def test_hosted_missing_configuration_makes_no_run(self) -> None:
        output = io.StringIO()
        with (
            patch("sys.argv", ["app.py", "--hosted"]),
            patch.object(EXAMPLE, "create_run") as create_run,
            redirect_stderr(output),
        ):
            self.assertEqual(EXAMPLE.main(), 2)
        create_run.assert_not_called()
        self.assertIn("KUMA_API_KEY", output.getvalue())
        self.assertIn("KUMA_REPO_PATH", output.getvalue())


if __name__ == "__main__":
    unittest.main()
