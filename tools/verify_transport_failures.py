"""Verify interrupted HTTP bodies retain the stable retryable error contract."""

from __future__ import annotations

import unittest
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from kuma.errors import ServiceError
from kuma.transport.backend import BackendClient


class _Response(BytesIO):
    status = 200

    def __init__(self, *, broken: bool = False) -> None:
        super().__init__(b"{}")
        self.broken = broken

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int | None = -1) -> bytes:
        if self.broken:
            raise IncompleteRead(b"partial", 10)
        return super().read(size)


def _client(*, retries: int = 0) -> BackendClient:
    return BackendClient(
        "dfx_test", base_url="https://example.test", timeout=1, max_retries=retries
    )


class TransportFailureTests(unittest.TestCase):
    @patch("kuma.transport.backend.schedule_update_check")
    @patch("kuma.transport.backend.urlopen", return_value=_Response(broken=True))
    def test_interrupted_success_body_is_stable_error(self, *_mocks) -> None:
        with self.assertRaises(ServiceError) as raised:
            _client().json("GET", "/sdk/probe/")

        self.assertEqual(raised.exception.code, "network_error")
        self.assertTrue(raised.exception.retryable)

    @patch("kuma.transport.backend.schedule_update_check")
    @patch("kuma.transport.backend.time.sleep")
    @patch(
        "kuma.transport.backend.urlopen",
        side_effect=(_Response(broken=True), _Response()),
    )
    def test_interrupted_body_is_retried(self, opened, *_mocks) -> None:
        self.assertEqual(_client(retries=1).json("GET", "/sdk/probe/"), {})
        self.assertEqual(opened.call_count, 2)

    @patch("kuma.transport.backend.urlopen")
    def test_interrupted_error_body_is_stable_error(self, opened) -> None:
        opened.side_effect = HTTPError(
            "https://example.test", 500, "error", Message(), _Response(broken=True)
        )

        with self.assertRaises(ServiceError) as raised:
            _client().json("GET", "/sdk/probe/")

        self.assertEqual(raised.exception.code, "network_error")
        self.assertTrue(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
