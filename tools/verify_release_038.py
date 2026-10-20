"""Credential-free 0.3.8 regression checks; no production transport or models."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from kuma import KumaClient, create_run
from kuma.config import resolve_create_run_config
from kuma.errors import ConfigurationError, LimitExceededError, ProviderError
from kuma.providers._official_evidence_upload import JudgeUploadConfig
from kuma.providers.official_judge import OfficialJudgeProvider
from kuma.repository import privacy
from kuma.repository.privacy import scan_sensitive_json
from kuma.transport.backend import _mapped_remote_error, _RemoteError
from kuma.transport.judge_limits import validate_judge_input_limit
from kuma.transport.operations import await_operation
from kuma.transport.request_records import RequestOperationStore

HANDLE = "kreq_" + "a" * 32
BUDGET = {"max_input_bytes": 2097152, "measurement": "final_judge_message_utf8"}


def store(root, handle=HANDLE, **changes):
    values = dict(
        request_type="case_generation",
        request_sha256="a" * 64,
        base_url="https://example.invalid",
        api_key_sha256="b" * 64,
        case_request_id=handle,
    )
    values.update(changes)
    return RequestOperationStore(root, **values)


class ReleaseChecks(unittest.TestCase):
    def test_short_authorization_instruction_boundary(self):
        for sentence in ("verify the payment method.", "check the account."):
            value = "**Payment authorization:** " + sentence
            for wrapped in (value, {"answer": value}):
                self.assertFalse(scan_sensitive_json(wrapped, location="output"))
                self.assertEqual(
                    privacy.redact_sensitive_json(wrapped), (wrapped, False)
                )
        for value in (
            "Authorization: verify the payment method.",
            "**Payment authorization:** bearer a secret.",
            "**Payment authorization:** basic the password.",
            "**Payment authorization:** custom a credential.",
            {"authorization": "check the account."},
            "**Payment authorization:** check the account.\npassword=x",
        ):
            self.assertTrue(scan_sensitive_json(value, location="output"))

    def test_conditional_reference_boundary(self):
        header = "if self.trust_env and not auth and not self.auth:\n    "
        safe = header + "auth = get_netrc_auth(request.url)"
        for wrapped in (safe, {"answer": safe}):
            self.assertFalse(scan_sensitive_json(wrapped, location="output"))
            self.assertEqual(privacy.redact_sensitive_json(wrapped), (wrapped, False))
        for value in (
            header + 'auth = "short-secret"',
            header + "auth = get_netrc_auth(request.url); password=x",
            "if (self.auth:\n    auth = get_netrc_auth(request.url)",
            "EXFIL_SECRET=1",
        ):
            self.assertTrue(scan_sensitive_json(value, location="output"))

    def test_multiline_privacy_context_and_index_cost(self):
        class LocalSlices(str):
            def __getitem__(self, key):
                if isinstance(key, slice) and len(range(*key.indices(len(self)))) > 256:
                    raise AssertionError("nonlocal context allocation")
                return super().__getitem__(key)

        class IndexedOnly(tuple):
            def __iter__(self):
                raise AssertionError("linear containment traversal")

        text = LocalSlices(
            "But here's the secret: the first draft is never the last.\n" * 300
        )
        for match in privacy._ASSIGNMENT.finditer(text):
            self.assertFalse(privacy._assignment_is_secret(match))
        spans = ((5, 9), (1, 4), (2, 3), (10, 20))
        index = privacy._index_spans(spans)
        guarded = privacy._SpanIndex(IndexedOnly(index.starts), IndexedOnly(index.ends))
        for start in range(22):
            for end in range(start, 23):
                self.assertEqual(
                    privacy._inside_spans((start, end), guarded),
                    any(a <= start and end <= b for a, b in spans),
                )
        for line in (
            "API_KEY=your_key\n",
            'token = os.environ.get("API_TOKEN")\n',
            "Authorization: Bearer <token>\n",
        ):
            value = line * 300
            self.assertFalse(scan_sensitive_json(value, location="output"))
            self.assertEqual(privacy.redact_sensitive_json(value), (value, False))
            self.assertTrue(
                scan_sensitive_json(
                    value + "password=literal-secret", location="output"
                )
            )

    def test_privacy_safe_context_and_adjacent_secret(self):
        safe = (
            "But here's the secret: the first draft is never the last.",
            "The model produces three vectors per token: query, key, value.",
            "LLM_API_KEY=your_llm_api_key",
            "Authorization: Bearer <token>",
            'token = os.environ.get("API_TOKEN")',
            'headers = {"Authorization": "Bearer " + api_key}',
            "load_model(use_auth_token=True)",
        )
        for value in safe:
            for wrapped in (value, {"answer": value}):
                self.assertFalse(scan_sensitive_json(wrapped, location="output"))
            self.assertTrue(
                scan_sensitive_json(
                    value + "\npassword=literal-secret", location="output"
                )
            )
        for value in ({"password": 1234}, "password=short", "sk_live_" + "a" * 24):
            self.assertTrue(scan_sensitive_json(value, location="output"))

    def test_optional_budget_and_closed_details(self):
        for value in ({}, {"judge_input_limit": BUDGET}):
            calls = []

            def transport(
                method, url, headers, body, timeout, calls=calls, value=value
            ):
                calls.append((method, dict(headers)))
                return value

            result = KumaClient(
                api_key="dfx_test.synthetic", transport=transport
            ).judge_config()
            self.assertEqual(result, value)
            self.assertEqual(calls[0][1]["X-Kuma-Evidence-Redaction"], "1")
        for value in (
            {**BUDGET, "private": "not-public"},
            {**BUDGET, "max_input_bytes": True},
        ):
            with self.assertRaises(ProviderError):
                validate_judge_input_limit(value)
        error = _mapped_remote_error(
            _RemoteError(
                413,
                {
                    "error": {
                        "code": "payload_too_large",
                        "retryable": False,
                        "details": BUDGET,
                    }
                },
            )
        )
        self.assertIsInstance(error, LimitExceededError)
        self.assertEqual(dict(error.details), BUDGET)

    def test_async_budget_get_only(self):
        with tempfile.TemporaryDirectory() as d:
            current = store(Path(d))
            state = current.load_or_create(lambda: "original")
            current.set_operation_id(state, "operation-1")
            client = Mock()
            client.json.return_value = {
                "operation_id": "operation-1",
                "status": "failed",
                "error": {
                    "code": "payload_too_large",
                    "retryable": False,
                    "details": BUDGET,
                },
            }
            start = Mock(side_effect=AssertionError("unexpected POST"))
            with self.assertRaises(LimitExceededError):
                await_operation(
                    client,
                    current,
                    key_factory=lambda: "wrong",
                    start=start,
                    wait_timeout=1,
                )
            start.assert_not_called()
            self.assertEqual(client.json.call_args.args[0], "GET")

    def test_redaction_negotiation_error(self):
        client = Mock()
        provider = OfficialJudgeProvider(client)
        provider._output_discovery = JudgeUploadConfig(
            2, 1000, 2000, "v1", 1, frozenset({"raw_log"})
        )
        with self.assertRaises(ProviderError) as caught:
            provider._prepare_agent_output("password=synthetic-secret")
        self.assertEqual(caught.exception.code, "runtime_evidence_unsupported")
        self.assertNotIn("synthetic-secret", str(caught.exception))
        client.json.assert_not_called()
        client.multipart.assert_not_called()

    def test_explicit_identity_terminal_reuse_isolation_and_capacity(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch("kuma.transport._request_files.MAX_RECORDS", 2),
        ):
            root = Path(d)
            current = store(root)
            state = current.load_or_create(lambda: "original")
            state = current.set_operation_id(state, "operation-1")
            current.mark_succeeded(state)
            self.assertEqual(
                store(root).load_or_create(lambda: "wrong").idempotency_key, "original"
            )
            store(root, None).load_or_create(lambda: "legacy")
            with self.assertRaises(LimitExceededError):
                store(root, "kreq_" + "b" * 32).load_or_create(lambda: "overflow")
            self.assertEqual(store(root).load().operation_id, "operation-1")
            with self.assertRaises(ProviderError):
                store(root, request_sha256="c" * 64).load_or_create(lambda: "wrong")

    def test_same_identity_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with ThreadPoolExecutor(max_workers=4) as pool:
                states = list(
                    pool.map(
                        lambda i: store(root).load_or_create(lambda: f"key-{i}"),
                        range(8),
                    )
                )
            self.assertEqual(len({state.idempotency_key for state in states}), 1)

    def test_high_and_unsupported_identity_options(self):
        self.assertTrue(
            resolve_create_run_config({"high_difficulty": True}).high_difficulty
        )
        with patch("kuma.api.BackendClient") as client:
            with self.assertRaises(ConfigurationError):
                create_run(case_request_id=HANDLE, case_provider=lambda _: [])
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
