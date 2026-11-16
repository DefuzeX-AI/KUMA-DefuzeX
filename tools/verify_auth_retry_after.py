"""Offline auth cooldown boundaries with real urllib error decoding and retries."""

import json
import tempfile
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from kuma.config import validate_api_key
from kuma.errors import KumaError, KumaTimeoutError, ServiceError
from kuma.transport.backend import BackendClient, _RemoteError
from kuma.transport.http import header_retry_after, retry_delay, safe_retry_after
from kuma.transport.operations import PendingOperationStore, await_operation


class Clock:
    """Advance fake monotonic time without real sleeps or external I/O."""

    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def _store(path):
    """Build disposable local operation metadata for GET-only recovery checks."""
    return PendingOperationStore(
        path,
        operation_type="case_generation",
        base_url="https://backend.example/api/agentdefuze",
    )


def auth_error(
    hint="3", *, code="api_key_service_unavailable", status=503, retryable=True
):
    headers = Message()
    if hint is not None:
        headers["Retry-After"] = hint
    return HTTPError(
        "https://backend.example/sdk/test/",
        status,
        "ignored",
        headers,
        BytesIO(json.dumps({"error": {"code": code, "retryable": retryable}}).encode()),
    )


class AuthRetryAfterTests(unittest.TestCase):
    def test_header_is_closed_bounded_and_duplicate_safe(self):
        for value in (1, 60, "1", "60"):
            self.assertEqual(safe_retry_after(value), int(value))
        for value in (
            None,
            True,
            0,
            61,
            -1,
            1.0,
            "0",
            "61",
            "9" * 5000,
            "01",
            " 2",
            "2 ",
            "1,2",
            "\uff11",  # Non-ASCII numeric input is not an HTTP delay-seconds value.
            "NaN",
            "Wed, 21 Oct 2026 00:00:00 GMT",
        ):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(safe_retry_after(value))
        self.assertIsNone(header_retry_after(None))
        self.assertIsNone(header_retry_after({}))
        self.assertEqual(header_retry_after({"rEtRy-AfTeR": "60"}), 60)
        headers = Message()
        headers.add_header("Retry-After", "1")
        headers.add_header("retry-after", "1")
        self.assertIsNone(header_retry_after(headers))
        with patch("kuma.transport.http.time.monotonic", return_value=10):
            self.assertEqual(retry_delay(0, None, 60), 60)
            self.assertEqual(retry_delay(5, None, 1), 2)
            self.assertEqual(retry_delay(0, 10.5, 60), 0.5)
            self.assertEqual(retry_delay(0, 9, 60), 0)
            self.assertEqual(retry_delay(0, None, 1000), 0.1)

    def test_http_auth_retries_keep_exact_post_and_attempt_budget(self):
        for path in ("/sdk/v2/cases/generate/", "/sdk/v2/judge/"):
            client = BackendClient(api_key="dfx_test", max_retries=2)
            clock = Clock()
            errors = [auth_error("3") for _ in range(3)]
            with (
                patch("kuma.transport.backend.urlopen", side_effect=errors) as opener,
                patch("time.monotonic", clock.monotonic),
                patch("time.sleep", clock.sleep),
                self.assertRaises(KumaError) as caught,
            ):
                client.json(
                    "POST",
                    path,
                    {"safe": True},
                    idempotency_key="same-key",
                    client_request_id="kreq_" + "a" * 32,
                )
            self.assertEqual(caught.exception.code, "api_key_service_unavailable")
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(clock.value, 6)
            self.assertEqual(opener.call_count, 3)
            requests = [call.args[0] for call in opener.call_args_list]
            self.assertEqual(len({r.data for r in requests}), 1)
            self.assertEqual(len({tuple(r.header_items()) for r in requests}), 1)
            self.assertTrue(all(error.closed for error in errors))
            self.assertEqual(dict(caught.exception.details), {})

    def test_only_retryable_503_auth_uses_hint_and_invalid_hint_falls_back(self):
        cases = [
            ("api_key_service_unavailable", 503, True, "60", 60, 2),
            ("api_key_service_unavailable", 503, True, "999999999", 0.1, 2),
            ("api_key_service_unavailable", 503, True, None, 0.1, 2),
            ("api_key_service_unavailable", 502, True, "60", 0.1, 2),
            ("api_key_service_unavailable", 503, False, "60", 0, 1),
            ("request_failed", 503, True, "60", 0, 1),
            ("invalid_api_key", 401, False, "60", 0, 1),
        ]
        for code, status, retryable, hint, elapsed, attempts in cases:
            with self.subTest(code=code, status=status, hint=hint, retryable=retryable):
                clock = Clock()
                client = BackendClient(api_key="dfx_test", max_retries=1)
                with (
                    patch(
                        "kuma.transport.backend.urlopen",
                        side_effect=[
                            auth_error(
                                hint, code=code, status=status, retryable=retryable
                            )
                            for _ in range(2)
                        ],
                    ) as opener,
                    patch("time.monotonic", clock.monotonic),
                    patch("time.sleep", clock.sleep),
                    self.assertRaises(KumaError),
                ):
                    client.json("GET", "/sdk/entitlements/")
                self.assertEqual(clock.value, elapsed)
                self.assertEqual(opener.call_count, attempts)

    def test_http_deadline_prevents_another_attempt_after_clipped_wait(self):
        clock = Clock()
        client = BackendClient(api_key="dfx_test", max_retries=2)
        with (
            patch(
                "kuma.transport.backend.urlopen", side_effect=auth_error("60")
            ) as opener,
            patch("time.monotonic", clock.monotonic),
            patch("time.sleep", clock.sleep),
            self.assertRaises(KumaTimeoutError),
        ):
            client.json("GET", "/sdk/entitlements/", _deadline=1.5)
        self.assertEqual(clock.value, 1.5)
        self.assertEqual(opener.call_count, 1)

    def test_auth_hint_does_not_leak_into_later_network_failure(self):
        clock = Clock()
        transport = Mock(
            side_effect=[
                _RemoteError(
                    503,
                    {
                        "error": {
                            "code": "api_key_service_unavailable",
                            "retryable": True,
                        }
                    },
                    retry_after=3,
                ),
                ServiceError("Unreachable", code="network_error", retryable=True),
                {"ok": True},
            ]
        )
        client = BackendClient(api_key="dfx_test", max_retries=2, transport=transport)
        with patch("time.monotonic", clock.monotonic), patch("time.sleep", clock.sleep):
            self.assertEqual(client.json("GET", "/sdk/entitlements/"), {"ok": True})
        self.assertEqual(clock.value, 3.2)
        self.assertEqual(transport.call_count, 3)

    def test_poll_honors_exhausted_http_hint_without_post_and_keeps_deadline(self):
        for wait_timeout in (2, 10):
            with tempfile.TemporaryDirectory() as directory:
                store = _store(Path(directory) / "pending.json")
                state = store.load_or_create(lambda: "stable-key")
                store.set_operation_id(state, "op-1")
                clock = Clock()
                transport = Mock(
                    side_effect=[
                        _RemoteError(
                            503,
                            {
                                "error": {
                                    "code": "api_key_service_unavailable",
                                    "retryable": True,
                                }
                            },
                            retry_after=3,
                        ),
                        {
                            "operation_id": "op-1",
                            "status": "succeeded",
                            "result": {"ok": True},
                        },
                    ]
                )
                client = BackendClient(
                    api_key="dfx_test", max_retries=0, transport=transport
                )
                start = Mock(side_effect=AssertionError("must not POST"))
                with (
                    patch("time.monotonic", clock.monotonic),
                    patch("time.sleep", clock.sleep),
                ):
                    if wait_timeout == 2:
                        with self.assertRaises(KumaTimeoutError):
                            await_operation(
                                client,
                                store,
                                key_factory=lambda: "unused",
                                start=start,
                                wait_timeout=wait_timeout,
                            )
                        self.assertTrue(store.path.exists())
                        self.assertEqual(transport.call_count, 1)
                    else:
                        self.assertEqual(
                            await_operation(
                                client,
                                store,
                                key_factory=lambda: "unused",
                                start=start,
                                wait_timeout=wait_timeout,
                            ),
                            {"ok": True},
                        )
                        self.assertEqual(transport.call_count, 2)
                self.assertEqual(clock.value, min(3, wait_timeout))
                self.assertTrue(
                    all(call.args[0] == "GET" for call in transport.call_args_list)
                )
                start.assert_not_called()

    def test_first_party_and_existing_keys_remain_opaque(self):
        for key in ("dfx_test", "dfx_k1_" + "a" * 16 + "." + "B" * 43):
            self.assertEqual(validate_api_key(key), key)


if __name__ == "__main__":
    unittest.main()
