"""Local authenticated OTLP/HTTP Trace ingestion for external Agent processes."""

from __future__ import annotations

import hmac
import secrets
import socket
import threading
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .errors import ConfigurationError
from .evidence.otlp_mapping import readable_span
from .evidence.trace import TraceEvidenceCapture
from .evidence.trace_mapping import SpanMappingError

_REQUEST_TIMEOUT = 2
_MAX_BATCH_SPANS = 1000


class _IngestError(Exception):
    """Carry a safe HTTP status without retaining external telemetry or header values."""

    def __init__(self, status: int) -> None:
        """Store only the response status for the local HTTP adapter."""
        self.status = status


def _inflate(body: bytes, ceiling: int) -> bytes:
    """Decode a single bounded gzip member and reject bombs, truncation and trailing data."""
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decoded = decoder.decompress(body, ceiling + 1)
    if len(decoded) > ceiling or decoder.unconsumed_tail:
        raise _IngestError(413)
    if not decoder.eof or decoder.unused_data:
        raise _IngestError(400)
    return decoded


class _DeadlineSocket(socket.socket):
    """Bound a request's total read time even when a client keeps sending bytes."""

    def __init__(self, connection: socket.socket) -> None:
        """Take ownership of the accepted connection and fix its absolute deadline."""
        super().__init__(
            connection.family,
            connection.type,
            connection.proto,
            fileno=connection.detach(),
        )
        self._deadline = time.monotonic() + _REQUEST_TIMEOUT

    def recv_into(self, buffer: Any, nbytes: int = 0, flags: int = 0) -> int:
        """Limit each SocketIO read to the time remaining in the original request."""
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OTLP request read deadline exceeded")
        self.settimeout(remaining)
        return super().recv_into(buffer, nbytes, flags)


class _ReceiverServer(HTTPServer):
    """Serve requests serially with bounded reads and no request-body diagnostics."""

    def get_request(self) -> Any:
        """Set a read deadline before the HTTP parser reads headers or the body."""
        connection, address = super().get_request()
        return _DeadlineSocket(connection), address

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Suppress external payload and credential details in unexpected server failures."""
        return None


class _Handler(BaseHTTPRequestHandler):
    """Accept Protobuf Trace batches only inside the receiver's authenticated step lease."""

    def log_message(self, format: str, *args: Any) -> None:
        """Keep local endpoint tokens, attributes and paths out of default HTTP logs."""
        return None

    def do_POST(self) -> None:
        """Read and map one bounded request, returning OTLP partial-success information."""
        receiver = self.server.receiver
        association = None
        try:
            with receiver._gate:
                receiver._authorize(self)
                association = receiver._lease[1]
                body = receiver._read(self)
                result = receiver._ingest(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except _IngestError as exc:
            if association is not None:
                reason = (
                    "trace_byte_limit" if exc.status == 413 else "trace_export_failed"
                )
                receiver.capture.record_failure(reason, association=association)
            self.send_error(exc.status)
        except (OSError, zlib.error, ValueError):
            if association is not None:
                receiver.capture.record_failure(
                    "trace_export_failed", association=association
                )
            self.send_error(400)


class OtlpTraceReceiver:
    """Receive external spans on loopback, binding each lease to one KUMA Input.

    Args:
        capture: Existing Trace capture supplied to the Run, or None to create
            one using default limits. Available afterward as ``self.capture``.
        port: IPv4 loopback TCP port, 0..65535; zero chooses a free port.
        max_request_bytes: Maximum compressed and decoded body bytes per
            batch, 1..16777216 inclusive; defaults to 8 MiB. Capture limits
            independently bound stored evidence. At most 1000 spans per batch.
    Returns:
        A receiver context manager; pass receiver.capture to create_run and
        call receiver.step() after get_input around each external invocation.
    Raises:
        ConfigurationError: Invalid values or missing ``otlp`` optional extra.
    Preconditions:
        Install ``kuma-defuzex[otlp]``. Use only with a trusted local exporter.
    Postconditions:
        No active lease or listener exists until context entry.
    Side Effects:
        Imports optional Protobuf definitions and allocates local state.
    Security/Privacy:
        Binds only 127.0.0.1. A separate ephemeral bearer authenticates each
        step; expose it only to that invocation. Existing sanitization runs
        before captured telemetry reaches local Run evidence or any upload.
    """

    def __init__(
        self,
        capture: TraceEvidenceCapture | None = None,
        *,
        port: int = 0,
        max_request_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        """Validate optional dependencies and initialize an inactive receiver."""
        if type(port) is not int or not 0 <= port <= 65535:
            raise ConfigurationError(
                "OTLP port must be an integer from 0 through 65535"
            )
        if (
            type(max_request_bytes) is not int
            or not 1 <= max_request_bytes <= 16 * 1024 * 1024
        ):
            raise ConfigurationError(
                "OTLP request limit must be 1 through 16777216 bytes"
            )
        if capture is not None and not isinstance(capture, TraceEvidenceCapture):
            raise ConfigurationError("OTLP capture must be a TraceEvidenceCapture")
        try:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                ExportTraceServiceRequest,
                ExportTraceServiceResponse,
            )
        except ImportError:
            raise ConfigurationError(
                "Install kuma-defuzex[otlp] to receive external Trace evidence"
            ) from None
        self.capture = capture if capture is not None else TraceEvidenceCapture()
        self._request_type = ExportTraceServiceRequest
        self._response_type = ExportTraceServiceResponse
        self._port = port
        self._max_bytes = max_request_bytes
        self._gate = threading.RLock()
        self._server: _ReceiverServer | None = None
        self._thread: threading.Thread | None = None
        self._lease: tuple[str, Any] | None = None

    def __enter__(self) -> OtlpTraceReceiver:
        """Start the loopback listener, or fail without retaining a socket on startup error."""
        with self._gate:
            if self._server is not None:
                raise ConfigurationError("OTLP receiver is already running")
            server = _ReceiverServer(("127.0.0.1", self._port), _Handler)
            server.receiver = self
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            try:
                thread.start()
            except BaseException:
                server.server_close()
                raise
            self._server, self._thread = server, thread
        return self

    def __exit__(self, *_exc: Any) -> None:
        """Revoke the step lease, stop the listener and join its bounded request thread."""
        with self._gate:
            self._lease = None
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()

    @contextmanager
    def step(self) -> Iterator[dict[str, str]]:
        """Yield standard OTLP exporter environment variables for the current Input.

        Args:
            None.
        Returns:
            A context manager yielding TRACES_ENDPOINT, TRACES_PROTOCOL and
            TRACES_HEADERS prefixed with OTEL_EXPORTER_OTLP_. Copy into the Java
            process environment, or configure its exporter with these values.
        Raises:
            ConfigurationError: The receiver is stopped, no Input is active,
                or another step lease exists.
        Preconditions:
            Call Run.get_input first with this receiver's capture installed.
            End and flush all invocation spans before leaving the context.
        Postconditions:
            The bearer is revoked at exit; late batches cannot enter another Input.
        Side Effects:
            Creates one bounded in-memory lease, waiting for in-flight ingestion
            at exit. Never modifies Run state or invokes an Agent automatically.
        Security/Privacy:
            The returned headers contain a local ephemeral secret. Do not log
            them, include them in Evidence, or pass them to an unrelated process.
        """
        with self.capture._lock:
            association = self.capture._active
        with self._gate:
            if self._server is None or association is None or not association.active:
                raise ConfigurationError(
                    "OTLP step requires a running receiver and an active Input"
                )
            if self._lease is not None:
                raise ConfigurationError("OTLP step lease is already active")
            token = secrets.token_urlsafe(32)
            lease = (token, association)
            self._lease = lease
            environment = {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{self._server.server_port}/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"Authorization=Bearer {token}",
            }
        try:
            yield environment
        finally:
            with self._gate:
                if self._lease is lease:
                    self._lease = None

    def _authorize(self, handler: _Handler) -> None:
        """Validate the exact path, one bearer and the original capture association."""
        if handler.path != "/v1/traces":
            raise _IngestError(404)
        headers = handler.headers.get_all("Authorization", [])
        lease = self._lease
        if (
            len(headers) != 1
            or lease is None
            or not headers[0].isascii()
            or not hmac.compare_digest(headers[0], "Bearer " + lease[0])
        ):
            raise _IngestError(401)
        if not lease[1].active:
            raise _IngestError(409)

    def _read(self, handler: _Handler) -> bytes:
        """Enforce a single length and bounded optional gzip before Protobuf decoding."""
        lengths = handler.headers.get_all("Content-Length", [])
        if handler.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
            raise _IngestError(400)
        if not lengths[0].isascii() or not lengths[0].isdecimal():
            raise _IngestError(400)
        size = int(lengths[0])
        if size > self._max_bytes:
            raise _IngestError(413)
        if handler.headers.get("Content-Type") != "application/x-protobuf":
            raise _IngestError(415)
        body = handler.rfile.read(size)
        if len(body) != size:
            raise _IngestError(400)
        encoding = handler.headers.get("Content-Encoding", "identity")
        if encoding == "gzip":
            return _inflate(body, self._max_bytes)
        if encoding != "identity":
            raise _IngestError(415)
        return body

    def _ingest(self, body: bytes) -> bytes:
        """Parse one bounded batch and reuse existing association-aware sanitizing capture."""
        from google.protobuf.message import DecodeError

        request = self._request_type()
        try:
            request.ParseFromString(body)
        except DecodeError:
            raise _IngestError(400) from None
        scopes = [
            (resource, scope)
            for resource in request.resource_spans
            for scope in resource.scope_spans
        ]
        if sum(len(scope.spans) for _, scope in scopes) > _MAX_BATCH_SPANS:
            raise _IngestError(413)
        association = self._lease[1]
        rejected = 0
        for resource, scope in scopes:
            for span in scope.spans:
                try:
                    readable = readable_span(span, resource.resource, scope.scope)
                except SpanMappingError as exc:
                    rejected += 1
                    self.capture.record_failure(exc.reason, association=association)
                    continue
                self.capture.export_span(readable, association=association)
        response = self._response_type()
        if rejected:
            response.partial_success.rejected_spans = rejected
            response.partial_success.error_message = "Invalid spans omitted"
        return response.SerializeToString()
