"""Exercise issue #58 through public Run APIs with synthetic, offline responses."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

from kuma import create_run
from kuma.errors import ProviderError
from kuma.run import Run
from kuma.transport.backend import BackendClient

GROUP_ID = "example-group"
BATCH_ID = "00000000-0000-4000-8000-000000000001"
RELEASE = "a" * 64
ACTUAL = {
    "schema_version": "kuma.executed_strategy_group.v1",
    "strategy_group_id": GROUP_ID,
    "strategy_group_version": "1",
    "catalog_release": RELEASE,
}
PROFILE = """---
agent_description: A deterministic offline example agent
input_type: text
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: example-group
  version: "1"
---

## Production Use Scenario
Answer one self-contained local test input.

## Behaviors to Test
Return the requested text exactly.

## Known Limitations or Prohibited Behaviors
Do not access the network or modify files.
"""


class OfflineBackend:
    """Serve a catalog and one resumable operation without external services."""

    def __init__(self) -> None:
        self.actual: Any = deepcopy(ACTUAL)
        self.include_actual = True
        self.calls: list[tuple[str, str]] = []
        self.request: dict[str, Any] = {}

    def __call__(
        self,
        method: str,
        url: str,
        _headers: Mapping[str, str],
        body: bytes | None,
        _timeout: float,
    ) -> Mapping[str, Any]:
        path = urlsplit(url).path
        self.calls.append((method, path))
        if (method, path) == ("GET", "/sdk/strategies/"):
            return {
                "schema_version": "kuma.strategy_group_catalog.v1",
                "catalog_release": RELEASE,
                "default": {"id": GROUP_ID, "version": "1"},
                "limits": {"max_selected_groups": 1},
                "groups": [
                    {
                        "id": GROUP_ID,
                        "version": "1",
                        "display_name": "Offline example",
                        "description": "Synthetic test catalog",
                        "available": True,
                        "required_capabilities": [],
                        "limits": {"max_steps": 10, "supported_difficulties": []},
                    }
                ],
            }
        if (method, path) == ("POST", "/sdk/v2/cases/generate/"):
            self.request = json.loads(body or b"{}")
            return {
                "operation_id": "operation-example",
                "status": "queued",
                "poll_after_ms": 100,
            }
        if (method, path) == ("GET", "/sdk/v2/operations/operation-example/"):
            return {
                "operation_id": "operation-example",
                "status": "succeeded",
                "result": self._result(),
            }
        raise AssertionError(f"Unexpected offline request: {method} {path}")

    def _result(self) -> dict[str, Any]:
        """Build the documented public Case checksum independently of SDK helpers."""
        fingerprint = self.request["repo_meta"]["repo_fingerprint"]
        case = {
            "schema_version": "2",
            "batch_id": BATCH_ID,
            "case_id": "case-example",
            "strategy_id": "coding",
            "strategy_version": "1",
            "repo_fingerprint": fingerprint,
            "title": "Offline example",
            "description": "One self-contained text input",
            "steps": [{"step_id": "step-1", "prompt": "Reply with exactly: ready"}],
        }
        canonical = json.dumps(
            case, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        case["signature"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
        result = {
            "batch": {
                "schema_version": "2",
                "batch_id": BATCH_ID,
                "case_ids": ["case-example"],
                "strategy_id": "coding",
                "strategy_version": "1",
                "repo_fingerprint": fingerprint,
                "requested_count": 1,
            },
            "cases": [case],
        }
        if self.include_actual:
            result["executed_strategy_group"] = deepcopy(self.actual)
        return result


class ExecutedStrategyGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="kuma-group-test-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        self.profile = self.repo / "agent-profile.md"
        self.profile.write_text(PROFILE, encoding="utf-8")
        self.backend = OfflineBackend()
        client = BackendClient(
            api_key="dfx_" + "a" * 40,
            base_url="https://example.invalid",
            transport=self.backend,
        )
        # Keep the real transport, normalization, runtime and request ledger;
        # replace only client construction to inject an offline wire transport.
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("kuma.api.BackendClient", return_value=client))
        stack.enter_context(
            patch(
                "kuma.transport.backend.urlopen",
                side_effect=AssertionError("Network is forbidden in this check"),
            )
        )

    def _create_run(self) -> Run:
        run = create_run(
            repo_path=self.repo,
            agent_profile_path=self.profile,
            judge=False,
            allow_local=True,
            track_files=False,
            save_local=False,
        )
        self.addCleanup(run.cancel)
        return run

    def test_actual_group_is_read_only_and_distinct_from_compatibility_id(self) -> None:
        run = self._create_run()
        self.assertEqual(run.executed_strategy_group, ACTUAL)
        self.assertEqual(
            self.backend.request["strategy_group_selection"]["strategy_group_id"],
            GROUP_ID,
        )
        calls = list(self.backend.calls)
        actual = run.executed_strategy_group
        self.assertIsNotNone(actual)
        with self.assertRaises(TypeError):
            actual["strategy_group_id"] = "changed"
        self.backend.actual["strategy_group_id"] = "changed"
        self.assertEqual(run.executed_strategy_group, ACTUAL)
        self.assertEqual(self.backend.calls, calls)
        self.assertEqual(run.get_input(), "Reply with exactly: ready")
        run.submit("ready")
        self.assertEqual(run.state, "completed")
        self.assertEqual(run.executed_strategy_group, ACTUAL)

    def test_historical_omission_does_not_echo_requested_group(self) -> None:
        self.backend.include_actual = False
        run = self._create_run()
        self.assertEqual(run.strategy, GROUP_ID)
        self.assertIsNone(run.executed_strategy_group)

    def test_default_selection_preserves_reported_group(self) -> None:
        self.profile.write_text(
            PROFILE.replace(
                "strategy_group:\n"
                "  schema_version: kuma.strategy_group_selection.v1\n"
                "  id: example-group\n"
                '  version: "1"\n',
                "",
            ),
            encoding="utf-8",
        )
        run = self._create_run()
        self.assertEqual(run.executed_strategy_group, ACTUAL)
        self.assertEqual(
            self.backend.request["strategy_group_selection"]["selection_source"],
            "general",
        )

    def test_custom_case_has_no_execution_group(self) -> None:
        run = create_run(
            repo_path=self.repo,
            agent_profile_path=self.profile,
            case_provider=lambda _context: "Reply with exactly: ready",
            max_steps=1,
            judge=False,
            allow_local=True,
            track_files=False,
            save_local=False,
        )
        self.addCleanup(run.cancel)
        self.assertIsNone(run.executed_strategy_group)
        self.assertEqual(self.backend.calls, [])

    def test_mismatched_coordinates_recover_without_another_case_post(self) -> None:
        for field, wrong in (
            ("strategy_group_id", "different-group"),
            ("strategy_group_version", "2"),
            ("catalog_release", "b" * 64),
        ):
            with self.subTest(field=field):
                self.backend.actual = {**ACTUAL, field: wrong}
                with self.assertRaises(ProviderError) as raised:
                    self._create_run().cancel()
                self.assertEqual(raised.exception.code, "invalid_response")
        self.backend.actual = deepcopy(ACTUAL)
        run = self._create_run()
        self.assertEqual(run.executed_strategy_group, ACTUAL)
        self.assertEqual(
            [call for call in self.backend.calls if call[0] == "POST"],
            [("POST", "/sdk/v2/cases/generate/")],
        )
        self.assertEqual(
            self.backend.calls.count(("GET", "/sdk/v2/operations/operation-example/")),
            4,
        )

    def test_null_malformed_and_extra_execution_fields_are_rejected(self) -> None:
        for actual in (
            None,
            {},
            {**ACTUAL, "schema_version": "unknown"},
            {**ACTUAL, "catalog_release": "not-a-digest"},
            {**ACTUAL, "selection_source": "user"},
        ):
            with self.subTest(actual=actual):
                self.backend.actual = actual
                with self.assertRaises(ProviderError) as raised:
                    self._create_run().cancel()
                self.assertEqual(raised.exception.code, "invalid_response")

    def test_saved_case_retains_actual_group_without_network(self) -> None:
        run = self._create_run()
        saved = run.save_case("case.json")
        run.cancel()
        calls = list(self.backend.calls)
        reused = create_run(
            repo_path=self.repo,
            case_path=saved,
            judge=False,
            allow_local=True,
            track_files=False,
            save_local=False,
        )
        self.addCleanup(reused.cancel)
        self.assertEqual(reused.executed_strategy_group, ACTUAL)
        self.assertEqual(reused.case_origin, "official")
        self.assertEqual(reused.case_id, run.case_id)
        self.assertNotEqual(reused.run_id, run.run_id)
        self.assertEqual(self.backend.calls, calls)


if __name__ == "__main__":
    unittest.main()
