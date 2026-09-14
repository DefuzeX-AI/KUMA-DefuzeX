"""Verify interrupted HTTP bodies retain the stable retryable error contract."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from email.message import Message
from http.client import BadStatusLine, HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
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
        self.close()
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


@contextmanager
def _server(responses):
    """Serve finite synthetic chunked responses on loopback; retain request identity."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._respond()

        def do_POST(self):
            self._respond()

        def _respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.headers.get("Idempotency-Key"), body))
            status, broken, request_id = responses[len(calls) - 1]
            self.send_response(status)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            if request_id is not None:
                self.send_header("X-Request-ID", request_id)
            self.end_headers()
            # Declare a chunk larger than the bytes actually sent, then close.
            # This exercises http.client's real bounded-read IncompleteRead path.
            self.wfile.write(
                b"20\r\npartial-private-fixture" if broken else b"2\r\n{}\r\n0\r\n\r\n"
            )
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class IntegratedTransportFailureTests(unittest.TestCase):
    def test_real_truncated_success_and_error_keep_current_safe_header(self):
        for status in (200, 500):
            for header in ("a" * 32, None, "invalid-id"):
                with self.subTest(status=status, header=header):
                    with (
                        _server([(status, True, header)]) as (url, calls),
                        self.assertRaises(ServiceError) as raised,
                    ):
                        BackendClient("dfx_test", base_url=url, max_retries=0).json(
                            "GET", "/sdk/probe/"
                        )
                    error = raised.exception
                    self.assertEqual(error.code, "network_error")
                    self.assertTrue(error.retryable)
                    self.assertEqual(
                        error.request_id, header if header == "a" * 32 else None
                    )
                    self.assertNotIn("partial-private-fixture", str(error))
                    self.assertEqual(len(calls), 1)

    @patch("kuma.transport.backend.time.sleep")
    @patch("kuma.transport.backend.schedule_update_check")
    def test_real_post_retry_preserves_key_and_body(self, *_mocks):
        for status in (200, 500):
            with self.subTest(status=status):
                with _server([(status, True, "a" * 32), (200, False, "b" * 32)]) as (
                    url,
                    calls,
                ):
                    client = BackendClient("dfx_test", base_url=url, max_retries=1)
                    result = client.json(
                        "POST",
                        "/sdk/probe/",
                        payload={"value": 1},
                        idempotency_key="same-task",
                    )
                self.assertEqual(result, {})
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0], calls[1])
                self.assertEqual(calls[0], ("POST", "same-task", b'{"value":1}'))

    def test_response_closed_for_success_and_http_error_read_failure(self):
        for status in (200, 500):
            with self.subTest(status=status):
                response = _Response(broken=True)
                response.headers = Message()
                response.headers["X-Request-ID"] = "a" * 32
                failure = HTTPError(
                    "https://example.test", status, "error", response.headers, response
                )
                with patch("kuma.transport.backend.urlopen") as opened:
                    if status == 200:
                        opened.return_value = response
                    else:
                        opened.side_effect = failure
                    with self.assertRaises(ServiceError) as raised:
                        _client().json("GET", "/sdk/probe/")
                self.assertTrue(response.closed)
                self.assertEqual(raised.exception.request_id, "a" * 32)

    def test_pre_header_protocol_failure_has_no_response_id_or_raw_chain(self):
        with (
            patch(
                "kuma.transport.backend.urlopen",
                side_effect=BadStatusLine("private-fixture"),
            ),
            self.assertRaises(ServiceError) as raised,
        ):
            _client().json("GET", "/sdk/probe/")
        self.assertEqual(raised.exception.code, "network_error")
        self.assertTrue(raised.exception.retryable)
        self.assertIsNone(raised.exception.request_id)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_user_interrupts_propagate_and_close_response_without_retry(self):
        for status in (200, 500):
            for kind in (KeyboardInterrupt, SystemExit):
                with self.subTest(status=status, kind=kind):
                    response = _Response()
                    interrupt = kind()
                    failure = HTTPError(
                        "https://example.test", status, "error", Message(), response
                    )
                    with (
                        patch.object(response, "read", side_effect=interrupt),
                        patch("kuma.transport.backend.urlopen") as opened,
                    ):
                        if status == 200:
                            opened.return_value = response
                        else:
                            opened.side_effect = failure
                        with self.assertRaises(kind) as raised:
                            _client(retries=2).json("GET", "/sdk/probe/")
                    self.assertIs(raised.exception, interrupt)
                    self.assertTrue(response.closed)
                    self.assertEqual(opened.call_count, 1)

    @patch("kuma.transport.backend.time.sleep")
    def test_exhausted_retries_keep_last_response_id(self, *_mocks):
        with (
            _server([(200, True, "a" * 32), (500, True, "b" * 32)]) as (url, calls),
            self.assertRaises(ServiceError) as raised,
        ):
            BackendClient("dfx_test", base_url=url, max_retries=1).json(
                "GET", "/sdk/probe/"
            )
        self.assertEqual(raised.exception.request_id, "b" * 32)
        self.assertEqual(len(calls), 2)

    def test_read_protocol_error_does_not_retain_partial_exception(self):
        response = _Response()
        with (
            patch.object(
                response, "read", side_effect=HTTPException("private-fixture")
            ),
            patch("kuma.transport.backend.urlopen", return_value=response),
            self.assertRaises(ServiceError) as raised,
        ):
            _client().json("GET", "/sdk/probe/")
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
