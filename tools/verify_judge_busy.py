"""Offline regressions for official Judge-only busy error presentation."""

from __future__ import annotations

import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from kuma.errors import KumaError, ServiceBusyError
from kuma.providers.official_judge import _batch_result, _JudgeUpload
from kuma.transport.backend import (
    BackendClient,
    _judge_error,
    _RemoteError,
    mapped_error,
)
from kuma.transport.operations import PendingOperationStore, await_operation

CODES = ("model_invalid_result", "model_invalid_response", "service_busy")


class JudgeBusyTests(unittest.TestCase):
    @patch("kuma.transport.backend.schedule_update_check")
    @patch("kuma.transport.backend.urlopen")
    def test_real_http_error_decode_keeps_header_not_private_body(
        self, opened, _update
    ):
        headers = Message()
        headers["X-Request-ID"] = "0123456789abcdef0123456789abcdef"
        opened.side_effect = HTTPError(
            "https://example.test/sdk/judge/",
            503,
            "private reason",
            headers,
            BytesIO(
                b'{"error":{"code":"model_invalid_result","retryable":false,'
                b'"message":"private text","details":{"reason":"invalid_format"}}}'
            ),
        )
        client = BackendClient("dfx_test", base_url="https://example.test")
        with self.assertRaises(KumaError) as caught:
            client.multipart("/sdk/judge/", {}, (), idempotency_key="same-key")
        self.assert_busy(caught.exception, False)
        self.assertEqual(
            caught.exception.request_id, "0123456789abcdef0123456789abcdef"
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(opened.call_count, 1)

    def assert_busy(self, error, retryable):
        self.assertIsInstance(error, ServiceBusyError)
        self.assertEqual(error.code, "service_busy")
        self.assertEqual(str(error), "Service is busy. Please try again later.")
        self.assertEqual(error.retryable, retryable)
        self.assertEqual(dict(error.details), {})

    def test_projection_preserves_retry_and_request_id_not_details(self):
        for code in CODES:
            for retryable in (False, True):
                original = KumaError(
                    "private text",
                    code=code,
                    retryable=retryable,
                    request_id="req-safe",
                    details={"reason": "private"},
                )
                error = _judge_error(original)
                self.assert_busy(error, retryable)
                self.assertEqual(error.request_id, "req-safe")
                self.assertEqual(str(original), "private text")

    def test_unrelated_errors_are_identical(self):
        for code in (
            "invalid_request",
            "invalid_api_key",
            "forbidden",
            "quota_exceeded",
            "sensitive_content",
            "invalid_response",
        ):
            error = mapped_error(code)
            self.assertIs(_judge_error(error), error)

    @patch("kuma.transport.backend.schedule_update_check")
    def test_http_judge_only_and_no_added_retries(self, _update):
        for path in (
            "/sdk/judge/",
            "/sdk/v2/judge/",
            "/sdk/judge/batch/",
            "/sdk/v2/cases/generate/",
        ):
            for code in CODES:
                for retryable in (False, True):
                    client = BackendClient(
                        "dfx_test", base_url="https://example.test", max_retries=3
                    )
                    transport = Mock(
                        side_effect=_RemoteError(
                            503,
                            {
                                "error": {
                                    "code": code,
                                    "retryable": retryable,
                                    "message": "服务繁忙，请稍后重试。",  # noqa: RUF001
                                }
                            },
                        )
                    )
                    client._transport = transport
                    with self.assertRaises(KumaError) as caught:
                        client.json("POST", path, {}, idempotency_key="same-key")
                    if "cases" in path:
                        self.assertEqual(caught.exception.code, code)
                    else:
                        self.assert_busy(caught.exception, retryable)
                    self.assertEqual(transport.call_count, 1)

    def test_failed_poll_judge_and_case_scope(self):
        for kind in ("judge", "judgment", "case_generation"):
            for code in CODES:
                for retryable in (False, True):
                    store = PendingOperationStore(
                        None, operation_type=kind, base_url="https://example.test"
                    )
                    client = Mock()
                    client.json.return_value = {
                        "operation_id": "op-test",
                        "status": "failed",
                        "error": {"code": code, "retryable": retryable},
                    }
                    start = Mock(
                        return_value={
                            "operation_id": "op-test",
                            "status": "queued",
                            "poll_after_ms": 100,
                        }
                    )
                    with self.assertRaises(KumaError) as caught:
                        await_operation(
                            client,
                            store,
                            key_factory=lambda: "same-key",
                            start=start,
                            wait_timeout=1,
                        )
                    if kind == "case_generation":
                        self.assertEqual(caught.exception.code, code)
                    else:
                        self.assert_busy(caught.exception, retryable)
                    self.assertEqual(start.call_count, 1)
                    self.assertEqual(client.json.call_count, 1)
                    self.assertIsNone(store.load())

    def test_poll_http_failure_retains_same_pending_operation(self):
        store = PendingOperationStore(
            None, operation_type="judgment", base_url="https://example.test"
        )
        state = store.load_or_create(lambda: "same-key")
        store.set_operation_id(state, "op-existing")
        client = Mock()
        client.json.side_effect = mapped_error("model_invalid_result", retryable=True)
        start = Mock()
        with self.assertRaises(KumaError) as caught:
            await_operation(
                client, store, key_factory=lambda: "unused", start=start, wait_timeout=1
            )
        self.assert_busy(caught.exception, True)
        start.assert_not_called()
        self.assertEqual(client.json.call_count, 1)
        self.assertEqual(store.load().operation_id, "op-existing")

    def test_batch_item_projection(self):
        upload = _JudgeUpload("run-test", {}, None, None, (), "same-key")
        for code in CODES:
            for retryable in (False, True):
                result = _batch_result(
                    upload,
                    {
                        "client_item_id": "run-test",
                        "ok": False,
                        "error": {"code": code, "retryable": retryable},
                    },
                )
                self.assert_busy(result.error, retryable)
                self.assertIsNone(result.report)


if __name__ == "__main__":
    unittest.main()
