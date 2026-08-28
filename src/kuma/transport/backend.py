"""Authenticated, retry-safe transport for the deployed KUMA SDK API."""

from __future__ import annotations

import json
import math
import secrets
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .._version import __version__
from ..config import resolve_api_key, validate_max_retries
from ..errors import (
    AuthenticationError,
    CaseIntegrityError,
    ConfigurationError,
    KumaError,
    KumaTimeoutError,
    LimitExceededError,
    PermissionDeniedError,
    RepoStateMismatchError,
    SensitiveDataError,
    ServiceBusyError,
    ServiceError,
    ValidationError,
)
from ..runtime import is_running_in_docker
from .http import (
    WireResponse,
    request_timeout,
    retry_delay,
    validate_request,
    validated_response,
)

DEFAULT_BASE_URL = "https://defuzex.ai/api/agentdefuze"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
WireTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    Mapping[str, Any],
]
_InternalWireTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    Mapping[str, Any] | WireResponse,
]


class _RemoteError(Exception):
    def __init__(self, status: int, payload: Mapping[str, Any] | None = None) -> None:
        self.status = status
        self.payload = dict(payload or {})
        super().__init__(f"HTTP {status}")


def _decode(raw: bytes, status: int) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _RemoteError(
            status,
            {"error": {"code": "invalid_response", "retryable": False}},
        ) from exc
    if not isinstance(value, Mapping):
        raise _RemoteError(
            status,
            {"error": {"code": "invalid_response", "retryable": False}},
        )
    return value


def _read_response(response: Any, status: int) -> Mapping[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ServiceError(
            "The KUMA response exceeded the SDK size limit.",
            code="response_too_large",
        )
    return _decode(raw, status)


def _wire_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> WireResponse:
    # Revalidate the final URL at the I/O boundary even when a custom caller
    # bypasses BackendClient's validated base_url invariant.
    _validate_base_url(url)
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            return WireResponse(
                status=response.status,
                payload=_read_response(response, response.status),
            )
    except HTTPError as exc:
        try:
            try:
                payload = _read_response(exc, exc.code)
            except _RemoteError as nested:
                payload = nested.payload
        finally:
            exc.close()
        raise _RemoteError(exc.code, payload) from None
    except TimeoutError as exc:
        raise KumaTimeoutError(
            "The KUMA request timed out.",
            code="network_timeout",
            retryable=True,
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise KumaTimeoutError(
                "The KUMA request timed out.",
                code="network_timeout",
                retryable=True,
            ) from exc
        raise ServiceError(
            "The KUMA service could not be reached.",
            code="network_error",
            retryable=True,
        ) from exc


def new_idempotency_key(operation: str) -> str:
    if (
        not operation
        or not operation.isascii()
        or not operation.replace("-", "").isalnum()
    ):
        raise ConfigurationError("operation must be a simple ASCII identifier")
    return f"sdk-v4-{operation}-{secrets.token_urlsafe(24)}"


def _validate_idempotency_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ConfigurationError(
            "idempotency_key must be 1-255 printable non-space ASCII characters"
        )
    return value


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("base_url must be an HTTPS URL")
    parsed_url = urlsplit(value)
    if (
        parsed_url.scheme not in {"https", "http"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ConfigurationError("base_url must be an HTTPS URL without credentials")
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    docker_host_gateway = (
        parsed_url.hostname == "host.docker.internal" and is_running_in_docker()
    )
    if (
        parsed_url.scheme == "http"
        and parsed_url.hostname not in loopback_hosts
        and not docker_host_gateway
    ):
        raise ConfigurationError(
            "HTTP base_url is allowed only for loopback development or the "
            "Docker host gateway from inside a container"
        )
    return value.rstrip("/")


def _validate_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError("timeout must be greater than zero")
    return float(value)


def _error_envelope(error: _RemoteError) -> tuple[str, bool, Mapping[str, Any]]:
    raw = error.payload.get("error")
    envelope = raw if isinstance(raw, Mapping) else {}
    raw_code = envelope.get("code")
    code = raw_code if isinstance(raw_code, str) and raw_code else "service_error"
    raw_retryable = envelope.get("retryable", False)
    retryable = raw_retryable if isinstance(raw_retryable, bool) else False
    # Backend details are deliberately not propagated: their shape is not part of
    # the public SDK contract and could accidentally contain internal context.
    return code, retryable, {}


_ERROR_CLASSES: dict[str, type[KumaError]] = {
    "invalid_api_key": AuthenticationError,
    "forbidden": PermissionDeniedError,
    "service_busy": ServiceBusyError,
    "upstream_unavailable": ServiceBusyError,
    "upload_not_configured": ServiceBusyError,
    "model_timeout": KumaTimeoutError,
    "model_invalid_result": ServiceError,
    "request_failed": ServiceError,
    "repo_state_mismatch": RepoStateMismatchError,
    "invalid_case_integrity": CaseIntegrityError,
    "log_size_exceeded": LimitExceededError,
    "payload_too_large": LimitExceededError,
    "quota_exhausted": LimitExceededError,
    "sensitive_content_detected": SensitiveDataError,
}
_VALIDATION_CODES = {
    "case_not_found",
    "idempotency_conflict",
    "invalid_case_file",
    "invalid_case_id",
    "invalid_case_reference",
    "invalid_content_length",
    "invalid_batch",
    "invalid_idempotency_key",
    "invalid_log_count",
    "invalid_log_parts",
    "invalid_manifest",
    "invalid_metadata",
    "invalid_request",
    "duplicate_log_name",
    "evidence_integrity_error",
    "sensitive_content_detected",
    "unsupported_log_type",
}
_STATUS_CLASSES: dict[int, type[KumaError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    408: KumaTimeoutError,
    413: LimitExceededError,
    429: LimitExceededError,
    504: KumaTimeoutError,
}
_ERROR_MESSAGES = {
    AuthenticationError: "The KUMA API key is invalid, expired, or revoked.",
    PermissionDeniedError: "The KUMA API key lacks permission for this operation.",
    ServiceBusyError: "The KUMA service is busy; submit a new request later.",
    KumaTimeoutError: "The KUMA model request timed out.",
    RepoStateMismatchError: "The repository state does not match the Case.",
    CaseIntegrityError: "The Case integrity metadata does not match.",
    LimitExceededError: "A KUMA service limit was exceeded.",
    SensitiveDataError: "Sensitive data was rejected by the KUMA service.",
    ValidationError: "The KUMA request was rejected as invalid.",
    ServiceError: "The KUMA service request failed.",
}


def _mapped_remote_error(error: _RemoteError) -> KumaError:
    code, retryable, details = _error_envelope(error)
    error_type = _ERROR_CLASSES.get(code) or _STATUS_CLASSES.get(error.status)
    if error_type is None:
        error_type = (
            ValidationError
            if error.status < 500 or code in _VALIDATION_CODES
            else ServiceError
        )
    return error_type(
        _ERROR_MESSAGES[error_type],
        code=code,
        retryable=retryable,
        details=details,
    )


def mapped_error(
    code: str,
    *,
    retryable: bool = False,
    status: int = 400,
) -> KumaError:
    """Map a safe public error envelope, including per-item batch failures."""

    return _mapped_remote_error(
        _RemoteError(
            status,
            {
                "error": {
                    "code": code,
                    "retryable": retryable,
                }
            },
        )
    )


@dataclass(frozen=True, slots=True)
class UploadPart:
    name: str
    filename: str
    content_type: str
    data: bytes

    def __post_init__(self) -> None:
        for label, value in (("name", self.name), ("filename", self.filename)):
            if not value or any(character in value for character in ('"', "\r", "\n")):
                raise ValidationError(f"multipart {label} is invalid")
        if not self.content_type or any(
            character in self.content_type for character in ("\r", "\n")
        ):
            raise ValidationError("multipart content_type is invalid")
        if not isinstance(self.data, bytes):
            raise ValidationError("multipart data must be bytes")


def encode_multipart(
    fields: Mapping[str, str], parts: Sequence[UploadPart]
) -> tuple[str, bytes]:
    boundary = "kuma-" + secrets.token_hex(18)
    chunks: list[bytes] = []
    for name, value in fields.items():
        if not name or any(character in name for character in ('"', "\r", "\n")):
            raise ValidationError("multipart field name is invalid")
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                str(value).encode("utf-8"),
                b"\r\n",
            )
        )
    for part in parts:
        chunks.extend(
            (
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{part.name}"; '
                    f'filename="{part.filename}"\r\n'
                ).encode("ascii"),
                f"Content-Type: {part.content_type}\r\n\r\n".encode("ascii"),
                part.data,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class BackendClient:
    """Authenticated transport restricted to the public Website Backend API.

    It accepts only ``/sdk/`` GET/POST paths. Retry attempts reuse the exact
    serialized request body and idempotency key, and remote details are mapped
    to stable public errors without propagating internal context.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
        transport: WireTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.timeout = _validate_timeout(timeout)
        self.max_retries = validate_max_retries(max_retries)
        self._api_key = resolve_api_key(api_key)
        self._transport: _InternalWireTransport = transport or _wire_transport

    def __repr__(self) -> str:
        return f"BackendClient(base_url={self.base_url!r}, authenticated=True)"

    def _headers(
        self, *, content_type: str, idempotency_key: str | None
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": content_type,
            "User-Agent": f"kuma-python/{__version__}",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _validate_idempotency_key(idempotency_key)
        return headers

    def _send(
        self,
        method: str,
        path: str,
        *,
        content_type: str,
        body: bytes | None,
        idempotency_key: str | None,
        deadline: float | None = None,
        expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        method = validate_request(method, path, idempotency_key)
        headers = self._headers(
            content_type=content_type,
            idempotency_key=idempotency_key,
        )
        attempts = 0
        while True:
            current_timeout = request_timeout(self.timeout, deadline)
            try:
                wire_response = self._transport(
                    method,
                    self.base_url + path,
                    headers,
                    body,
                    current_timeout,
                )
                return validated_response(wire_response, expected_status)
            except _RemoteError as exc:
                error = _mapped_remote_error(exc)
            except KumaError as exc:
                error = exc
            if (
                error.retryable
                and not isinstance(error, ServiceBusyError)
                and attempts < self.max_retries
            ):
                delay = retry_delay(attempts, deadline)
                attempts += 1
                time.sleep(delay)
                continue
            raise error from None

    def json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        _deadline: float | None = None,
        _expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        try:
            body = (
                None
                if payload is None
                else json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("Backend JSON payload must be serializable") from exc
        return self._send(
            method.upper(),
            path,
            content_type="application/json",
            body=body,
            idempotency_key=idempotency_key,
            deadline=_deadline,
            expected_status=_expected_status,
        )

    def multipart(
        self,
        path: str,
        fields: Mapping[str, str],
        parts: Sequence[UploadPart],
        *,
        idempotency_key: str,
        _deadline: float | None = None,
        _expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        content_type, body = encode_multipart(fields, parts)
        return self._send(
            "POST",
            path,
            content_type=content_type,
            body=body,
            idempotency_key=idempotency_key,
            deadline=_deadline,
            expected_status=_expected_status,
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "BackendClient",
    "UploadPart",
    "WireTransport",
    "encode_multipart",
    "mapped_error",
    "new_idempotency_key",
]
