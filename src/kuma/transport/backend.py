"""Authenticated, retry-safe transport for the deployed KUMA SDK API."""

from __future__ import annotations

import hashlib
import json
import math
import re
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
from ..config import DEFAULT_CASE_MAX_STEPS, resolve_api_key, validate_max_retries
from ..errors import (
    AuthenticationError,
    CaseIntegrityError,
    ConfigurationError,
    KumaError,
    KumaTimeoutError,
    LimitExceededError,
    PermissionDeniedError,
    ProviderError,
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
_CLIENT_REQUEST_ID_PATTERN = re.compile(r"kreq_[0-9a-f]{32}\Z")
WireTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    Mapping[str, Any],
]
_InternalWireTransport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    Mapping[str, Any] | WireResponse,
]


class _RemoteError(Exception):
    """Hold a bounded remote failure until it is mapped to a public SDK error."""

    def __init__(self, status: int, payload: Mapping[str, Any] | None = None) -> None:
        """Retain an HTTP failure for later conversion to a public SDK error.

        Args:
            status: HTTP status code returned by the public Backend.
            payload: Parsed public JSON error object. ``None`` means no usable
                response object was available.

        Preconditions:
            ``status`` comes from the HTTP boundary; ``payload`` has already
            passed the bounded response reader when supplied.

        Postconditions:
            The payload is detached into a mutable private copy. The exception
            message contains only the status code and never response text.

        Side Effects:
            None beyond initializing the exception.

        Security/Privacy:
            Callers must map this internal exception through
            :func:`_mapped_remote_error` before exposing it to SDK users.
        """
        self.status = status
        self.payload = dict(payload or {})
        super().__init__(f"HTTP {status}")


def _decode(raw: bytes, status: int) -> Mapping[str, Any]:
    """Decode a bounded response as the required top-level JSON object.

    Args:
        raw: Complete response bytes already limited by :func:`_read_response`.
        status: HTTP status associated with those bytes.

    Returns:
        Parsed JSON mapping. Arrays, scalars, and ``null`` are rejected because
        every public Backend response must be an object.

    Raises:
        _RemoteError: UTF-8 decoding, JSON parsing, or top-level shape fails. Its
            replacement payload uses only stable ``invalid_response`` fields.

    Preconditions:
        ``raw`` is no larger than ``_MAX_RESPONSE_BYTES``.

    Postconditions:
        Success returns the decoded mapping without changing it. Failure retains
        no raw response text in the public error envelope.

    Security/Privacy:
        Malformed server content is deliberately replaced rather than echoed.
    """
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
    """Read and decode one HTTP response without permitting unbounded memory use.

    Args:
        response: File-like HTTP response exposing ``read(size)``.
        status: HTTP status used if JSON decoding must raise ``_RemoteError``.

    Returns:
        Parsed top-level JSON mapping from the bounded response body.

    Raises:
        ServiceError: The body exceeds the 8 MiB SDK response limit.
        _RemoteError: The bounded body is not a valid UTF-8 JSON object.

    Preconditions:
        The caller owns the response lifecycle and will close it.

    Postconditions:
        Reads at most ``_MAX_RESPONSE_BYTES + 1`` bytes, which is sufficient to
        distinguish an allowed body from an oversized one.

    Side Effects:
        Advances the response stream to the consumed position.

    Security/Privacy:
        Raw response bytes never appear in raised SDK messages.
    """
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
    """Perform exactly one HTTP attempt against the validated public Backend URL.

    Args:
        method: Uppercase HTTP method already accepted by ``validate_request``.
        url: Final absolute Backend URL, including the validated SDK path.
        headers: Complete HTTP headers, including authentication and optional
            idempotency key. Values must already be header-safe.
        body: Exact serialized request bytes, or ``None`` for a bodyless request.
        timeout: Positive per-attempt timeout in seconds.

    Returns:
        ``WireResponse`` containing the status and parsed JSON object for a 2xx
        response. Status interpretation remains the caller's responsibility.

    Raises:
        _RemoteError: The server returns an HTTP error response.
        KumaTimeoutError: The socket or URL layer reaches ``timeout``.
        ServiceError: The service is unreachable or its response is oversized.

    Preconditions:
        ``BackendClient`` has validated the base URL, method, SDK path, body
        size, timeout, API key, and idempotency key. This low-level function must
        not be called with an arbitrary user URL.

    Postconditions:
        Exactly one network attempt has occurred. Every opened response or
        ``HTTPError`` stream is closed before return or raise. No retry occurs
        here; retry ownership remains in ``BackendClient._send``.

    Side Effects:
        Sends one HTTPS request, or approved loopback/Docker-gateway HTTP request.

    Security/Privacy:
        Response text, provider details, and tracebacks are never interpolated
        into user-visible messages. The Authorization header is not logged.
    """
    request = Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
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
    """Create one unpredictable HTTP idempotency key for a logical operation.

    Args:
        operation: Short ASCII operation label such as ``"case"`` or
            ``"judge"``. Letters, digits, and hyphens are accepted.

    Returns:
        Header-safe key beginning with ``sdk-v4-<operation>-`` and containing a
        cryptographically random suffix.

    Raises:
        ConfigurationError: ``operation`` is empty, non-ASCII, or contains a
            character other than a letter, digit, or hyphen.

    Preconditions:
        Call once when creating a new logical operation, then persist/reuse the
        returned value for retries and resume.

    Postconditions:
        Produces a new opaque value; it contains no API key or request payload.

    Security/Privacy:
        The key is correlation metadata, not a credential, but applications
        should still avoid exposing it unnecessarily.
    """
    if (
        not operation
        or not operation.isascii()
        or not operation.replace("-", "").isalnum()
    ):
        raise ConfigurationError("operation must be a simple ASCII identifier")
    return f"sdk-v4-{operation}-{secrets.token_urlsafe(24)}"


def _validate_idempotency_key(value: str) -> str:
    """Validate a caller-stable idempotency key before header construction.

    Args:
        value: Candidate key supplied by the operation/resume layer.

    Returns:
        The unchanged key after validating 1-255 printable, non-space ASCII
        characters.

    Raises:
        ConfigurationError: The value is not text or is empty, oversized,
            non-ASCII, whitespace-containing, or control-character-containing.

    Preconditions:
        The caller intends to place this value in ``Idempotency-Key``.

    Postconditions:
        Success guarantees direct HTTP header representation without newline or
        delimiter injection.
    """
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


def validate_client_request_id(value: str) -> str:
    """Validate the public request-recovery identifier used by v2 endpoints.

    Args:
        value: Candidate identifier generated by KUMA or supplied to the public
            recovery lookup API.

    Returns:
        The unchanged identifier when it matches ``kreq_`` plus 128-bit lower-
        case hexadecimal entropy.

    Raises:
        ValidationError: If ``value`` is not the exact frozen wire shape.

    Security/Privacy:
        Validation occurs before headers or URLs are constructed, preventing
        control-character injection and accidental routing to another endpoint.
    """
    if (
        not isinstance(value, str)
        or _CLIENT_REQUEST_ID_PATTERN.fullmatch(value) is None
    ):
        raise ValidationError(
            "client_request_id is invalid", code="invalid_client_request_id"
        )
    return value


def _validate_base_url(value: str) -> str:
    """Validate the public Backend origin and normalize only its trailing slash.

    Args:
        value: Absolute public API base URL. Remote services require HTTPS;
            loopback HTTP is allowed for local integration, and the Docker host
            gateway is allowed only from a detected container.

    Returns:
        The same URL with trailing slashes removed. Path spelling and routing are
        otherwise preserved.

    Raises:
        ConfigurationError: The URL is non-text, lacks scheme/host, embeds
            credentials, query, or fragment, uses another scheme, or requests
            remote insecure HTTP.

    Preconditions:
        ``value`` identifies the public Website Backend, never MCP or a provider.

    Postconditions:
        Concatenating a validated ``/sdk/...`` path produces an HTTP(S) URL that
        satisfies the SDK transport policy.

    Security/Privacy:
        Embedded username/password data and arbitrary local-file schemes are
        rejected before any request occurs.
    """
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
    """Normalize a per-request timeout without accepting booleans or infinity.

    Args:
        value: Integer or floating-point duration in seconds.

    Returns:
        Equivalent positive finite ``float``.

    Raises:
        ConfigurationError: The value is boolean, non-numeric, non-finite, zero,
            or negative.

    Postconditions:
        The result is safe to pass to the HTTP transport as a blocking timeout.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError("timeout must be greater than zero")
    return float(value)


def _error_envelope(
    error: _RemoteError,
) -> tuple[str, bool, str | None, Mapping[str, Any]]:
    """Project a remote failure onto the closed SDK-safe error envelope.

    Args:
        error: Internal HTTP failure containing a parsed response object.

    Returns:
        ``(code, retryable, message, details)`` where arbitrary details are
        discarded and only the closed Case step-limit field may survive.

    Preconditions:
        ``error`` has not yet crossed the public SDK exception boundary.

    Postconditions:
        Only typed ``code``, ``retryable``, optional text ``message``, and the
        integer ``max_allowed_steps`` for ``case_step_limit_exceeded`` remain.

    Security/Privacy:
        Backend/Core/provider diagnostics cannot flow through ``details``.
    """
    raw = error.payload.get("error")
    envelope = raw if isinstance(raw, Mapping) else {}
    raw_code = envelope.get("code")
    code = raw_code if isinstance(raw_code, str) and raw_code else "service_error"
    raw_retryable = envelope.get("retryable", False)
    retryable = raw_retryable if isinstance(raw_retryable, bool) else False
    raw_message = envelope.get("message")
    message = raw_message if isinstance(raw_message, str) else None
    return code, retryable, message, _case_step_limit_details(code, envelope)


def _case_step_limit_details(
    code: str, envelope: Mapping[str, Any]
) -> Mapping[str, int]:
    """Validate the sole public error-detail shape allowed through the SDK.

    Arbitrary Backend details remain discarded for every other error code. The
    Case limit code fails closed when its required details object is absent,
    malformed, or contains an unknown field.

    Args:
        code: Stable public error code already reduced to a string.
        envelope: Parsed Backend error mapping at the HTTP boundary.

    Returns:
        Immutable-by-convention mapping containing only ``max_allowed_steps``,
        or an empty mapping for all unrelated codes.

    Raises:
        ProviderError: If the Case-limit details are absent, out of range,
            incorrectly typed, or contain any additional field.

    Security/Privacy:
        No arbitrary Backend/Core/provider diagnostic field crosses this helper.
    """
    if code != "case_step_limit_exceeded":
        return {}
    details = envelope.get("details")
    if not isinstance(details, Mapping) or set(details) != {"max_allowed_steps"}:
        raise ProviderError(
            "The Backend returned invalid Case step-limit details",
            code="invalid_response",
        )
    maximum = details["max_allowed_steps"]
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= DEFAULT_CASE_MAX_STEPS
    ):
        raise ProviderError(
            "The Backend returned invalid Case step-limit details",
            code="invalid_response",
        )
    return {"max_allowed_steps": maximum}


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
    "case_step_limit_exceeded": LimitExceededError,
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
_CODE_ERROR_MESSAGES = {
    "forbidden": "此密钥没有权限使用该功能。",
    "idempotency_conflict": "这个请求已经提交过了，请不要重复提交。",  # noqa: RUF001
    "invalid_api_key": "密钥无效。",
    "invalid_request": "提交的内容有问题，请检查后重试。",  # noqa: RUF001
}
_NOT_FOUND_MESSAGE = "你要找的内容不存在，或者已经被删除。"  # noqa: RUF001
_READ_ONLY_FORBIDDEN_MESSAGE = "此密钥为只读密钥，无法生成 Case 或运行 Judge。"  # noqa: RUF001


def _public_error_message(
    code: str,
    error_type: type[KumaError],
    remote_message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Select a frozen user-facing message without trusting arbitrary server text.

    Args:
        code: Stable public error code used by program logic.
        error_type: SDK exception class selected for the code/status.
        remote_message: Optional Backend message from the closed error envelope.
        details: Closed safe details for codes that define them.

    Returns:
        Canonical SDK wording, except for an exact match to an approved frozen
        Backend message (including the read-only ``forbidden`` specialization).

    Preconditions:
        ``error_type`` exists in ``_ERROR_MESSAGES`` and ``code`` has already
        been reduced to a non-empty string.

    Postconditions:
        The returned message is always from the SDK allowlist; unrecognized
        remote wording cannot become user-visible.

    Security/Privacy:
        Prevents provider, database, path, traceback, or balance details from
        leaking through an otherwise valid error object.
    """

    if code == "case_step_limit_exceeded" and details:
        return (
            "max_steps exceeds the Case service limit; maximum allowed is "
            f"{details['max_allowed_steps']}."
        )
    if code == "resource_not_found" or code.endswith("_not_found"):
        canonical = _NOT_FOUND_MESSAGE
    else:
        canonical = _CODE_ERROR_MESSAGES.get(code, _ERROR_MESSAGES[error_type])
    # Backend may send the same frozen public wording so async and synchronous
    # errors retain it exactly. Any other text falls back to the SDK mapping;
    # this prevents an upstream detail or traceback from becoming user-visible.
    if remote_message == canonical or (
        code == "forbidden" and remote_message == _READ_ONLY_FORBIDDEN_MESSAGE
    ):
        return remote_message
    return canonical


def _mapped_remote_error(error: _RemoteError) -> KumaError:
    """Convert one internal HTTP failure into the stable public exception model.

    Args:
        error: Internal response failure with status and parsed public envelope.

    Returns:
        Unraised ``KumaError`` subclass carrying safe message, stable code,
        retryability, and an empty detached details mapping.

    Preconditions:
        The response has already passed size and top-level JSON validation.

    Postconditions:
        Authentication, permission, timeout, limit, validation, integrity,
        service-busy, and generic service failures use deterministic classes.

    Security/Privacy:
        Raw response content and unapproved remote messages are absent.
    """
    code, retryable, message, details = _error_envelope(error)
    error_type = _ERROR_CLASSES.get(code) or _STATUS_CLASSES.get(error.status)
    if error_type is None:
        error_type = (
            ValidationError
            if error.status < 500 or code in _VALIDATION_CODES
            else ServiceError
        )
    return error_type(
        _public_error_message(code, error_type, message, details),
        code=code,
        retryable=retryable,
        details=details,
    )


def mapped_error(
    code: str,
    *,
    retryable: bool = False,
    status: int = 400,
    message: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> KumaError:
    """Map already separated public error fields through the HTTP error policy.

    Args:
        code: Stable public error code.
        retryable: Whether repeating the same logical operation may succeed.
        status: HTTP-equivalent status used for class fallback; defaults to 400.
        message: Optional frozen public wording. Arbitrary text is ignored by
            :func:`_public_error_message`.
        details: Optional closed public error details. Only the Case step-limit
            code may carry ``max_allowed_steps``.

    Returns:
        Unraised public ``KumaError`` suitable for a batch item or asynchronous
        failed-operation result.

    Preconditions:
        Callers have validated the closed error schema and field types.

    Postconditions:
        Produces the same class/message policy as a synchronous HTTP failure.

    Security/Privacy:
        Discards all details except the validated integer Case service ceiling.
    """

    envelope: dict[str, Any] = {
        "code": code,
        "retryable": retryable,
    }
    if message is not None:
        envelope["message"] = message
    if details is not None:
        envelope["details"] = dict(details)
    return _mapped_remote_error(
        _RemoteError(
            status,
            {"error": envelope},
        )
    )


@dataclass(frozen=True, slots=True)
class UploadPart:
    """Describe one validated immutable multipart Evidence part.

    Attributes:
        name: Safe multipart form field name.
        filename: Safe display filename without CR/LF or quote injection.
        content_type: Declared MIME type used by the public Judge contract.
        data: Already bounded and privacy-checked bytes to serialize.

    Security/Privacy:
        Construction validates framing but does not itself authorize ``data``;
        callers must complete size and sensitive-data checks first.
    """

    name: str
    filename: str
    content_type: str
    data: bytes

    def __post_init__(self) -> None:
        """Validate one immutable multipart upload part before serialization.

        Raises:
            ValidationError: Name, filename, or content type is empty or permits
                header injection, or ``data`` is not bytes.

        Preconditions:
            The caller has already bounded and privacy-checked the part content.

        Postconditions:
            Success guarantees that ``encode_multipart`` can insert metadata into
            MIME headers without quote or CR/LF injection.

        Security/Privacy:
            This validates framing only; it does not authorize the bytes for
            upload or scan their content.
        """
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
    """Serialize public fields and upload parts as one multipart/form-data body.

    Args:
        fields: Text form fields keyed by header-safe field names.
        parts: Ordered, validated ``UploadPart`` objects.

    Returns:
        ``(content_type, body)`` using a fresh random MIME boundary. Part order
        matches the input sequence.

    Raises:
        ValidationError: A field name is empty or permits quote/CR/LF injection.
            ``UploadPart`` metadata errors are rejected at construction time.

    Preconditions:
        Field values and part bytes have already passed size, schema, and
        sensitive-data policy for the target public endpoint.

    Postconditions:
        Returns a complete closing-boundary-terminated body; inputs are unchanged.

    Security/Privacy:
        Random boundaries prevent caller-controlled delimiter collisions. This
        function performs no network request and no content redaction.
    """
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
        """Create a validated, authenticated client for public SDK endpoints.

        Args:
            api_key: Per-client ``dfx_`` credential. ``None`` resolves
                ``KUMA_API_KEY`` and then the user credential file.
            base_url: Public Backend API base URL; remote addresses require HTTPS.
            timeout: Positive finite timeout in seconds for each HTTP attempt.
            transport: Optional controlled/test transport. ``None`` uses the
                standard-library HTTP implementation.
            max_retries: Additional retries for eligible transient failures,
                from 0 through the configured SDK maximum.

        Raises:
            ConfigurationError: URL, timeout, retry count, or credential format
                is invalid.
            AuthenticationError: No credential can be resolved.

        Preconditions:
            ``base_url`` points to the public Website Backend, never private MCP.

        Postconditions:
            The client stores only validated configuration and performs no
            network request until ``json`` or ``multipart`` is called.

        Side Effects:
            May read the environment or user credential file.

        Security/Privacy:
            The API key remains private client state and is omitted from repr and
            public exceptions.
        """
        self.base_url = _validate_base_url(base_url)
        self.timeout = _validate_timeout(timeout)
        self.max_retries = validate_max_retries(max_retries)
        self._api_key = resolve_api_key(api_key)
        self._transport: _InternalWireTransport = transport or _wire_transport

    def __repr__(self) -> str:
        """Return a diagnostic representation containing no credential value.

        Returns:
            Text containing only the validated base URL and authenticated flag.

        Postconditions:
            The API key, request bodies, and idempotency keys are absent.
        """
        return f"BackendClient(base_url={self.base_url!r}, authenticated=True)"

    @property
    def credential_identity(self) -> str:
        """Return a one-way identity for binding local recovery state to this key.

        Returns:
            SHA-256 of the validated API key. The digest can distinguish a key
            rotation without exposing the credential itself.

        Security/Privacy:
            This property performs no I/O and never returns the raw key. The
            digest is used only in owner-readable local request records.
        """
        return hashlib.sha256(self._api_key.encode("ascii")).hexdigest()

    def _headers(
        self,
        *,
        content_type: str,
        idempotency_key: str | None,
        client_request_id: str | None,
    ) -> dict[str, str]:
        """Build the complete header set for one public SDK request.

        Args:
            content_type: MIME type matching the already serialized body.
            idempotency_key: Stable logical-operation key, or ``None`` for a read
                or other request that does not require replay protection.
            client_request_id: Optional public recovery identity for v2 Case or
                Judge start requests.

        Returns:
            New header dictionary containing Accept, Authorization, Content-Type,
            User-Agent, and optional Idempotency-Key.

        Raises:
            ConfigurationError: The idempotency key is not header-safe.

        Preconditions:
            The client contains a validated API key and ``content_type`` is owned
            by the JSON or multipart serializer.

        Postconditions:
            Each call returns a fresh mapping; modifying it cannot mutate client
            configuration.

        Security/Privacy:
            The returned mapping contains a live Bearer credential and must not
            be logged or persisted.
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": content_type,
            "User-Agent": f"kuma-python/{__version__}",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _validate_idempotency_key(idempotency_key)
        if client_request_id is not None:
            headers["X-Kuma-Client-Request-Id"] = validate_client_request_id(
                client_request_id
            )
        return headers

    def _send(
        self,
        method: str,
        path: str,
        *,
        content_type: str,
        body: bytes | None,
        idempotency_key: str | None,
        client_request_id: str | None = None,
        deadline: float | None = None,
        expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        """Execute a bounded public request under the SDK retry/deadline policy.

        Args:
            method: HTTP method accepted by the public transport validator.
            path: Relative ``/sdk/...`` endpoint; absolute or private paths fail.
            content_type: MIME type corresponding to ``body``.
            body: Exact serialized bytes, or ``None``.
            idempotency_key: Stable key reused byte-for-byte across every retry.
            client_request_id: Optional stable recovery identity reused across
                every retry of the same accepted v2 start request.
            deadline: Optional monotonic absolute deadline shared by operation
                polling; ``None`` uses only the per-attempt timeout.
            expected_status: Optional exact successful status required by the
                asynchronous operation contract.

        Returns:
            Validated public JSON mapping from the first successful attempt.

        Raises:
            KumaError: Validation, authentication, permission, timeout, limit,
                service, malformed-response, or deadline handling fails.

        Preconditions:
            Body serialization and privacy checks are complete. Mutating requests
            carry the same idempotency key for the same logical operation.

        Postconditions:
            At most ``max_retries + 1`` attempts occur. Every retry uses identical
            method, URL, headers, body, and idempotency key. ``ServiceBusyError``
            is returned immediately to avoid amplifying server load.

        Side Effects:
            Performs network I/O and sleeps for bounded transient backoff.

        Security/Privacy:
            Sends only to the validated public base URL plus validated SDK path;
            mapped exceptions never include the credential or raw response.
        """
        method = validate_request(method, path, idempotency_key)
        if client_request_id is not None and (
            method != "POST"
            or path not in {"/sdk/v2/cases/generate/", "/sdk/v2/judge/"}
        ):
            raise ConfigurationError(
                "client_request_id is only valid for v2 Case or Judge start requests"
            )
        headers = self._headers(
            content_type=content_type,
            idempotency_key=idempotency_key,
            client_request_id=client_request_id,
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
        client_request_id: str | None = None,
        _deadline: float | None = None,
        _expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        """Serialize and send one deterministic public JSON request.

        Args:
            method: Public HTTP method; normalized to uppercase.
            path: Relative ``/sdk/...`` endpoint.
            payload: JSON-compatible mapping, or ``None`` for no body.
            idempotency_key: Stable replay key for a mutating logical operation.
            client_request_id: Optional v2 request-recovery identity.
            _deadline: Internal monotonic operation deadline propagated to
                transport retries.
            _expected_status: Internal exact-success status constraint.

        Returns:
            Validated public JSON response mapping.

        Raises:
            ValidationError: ``payload`` is not finite JSON serializable.
            KumaError: Request validation, transport, status, or response fails.

        Preconditions:
            Payload fields satisfy the endpoint's public schema and privacy rules.

        Postconditions:
            A non-``None`` payload is encoded as compact UTF-8 JSON with stable
            insertion order and no NaN/Infinity. Retries reuse those exact bytes.

        Side Effects:
            Delegates network I/O and bounded retries to :meth:`_send`.
        """
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
            client_request_id=client_request_id,
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
        client_request_id: str | None = None,
        _deadline: float | None = None,
        _expected_status: int | None = None,
    ) -> Mapping[str, Any]:
        """Serialize and upload bounded public Evidence as multipart form data.

        Args:
            path: Relative public Judge endpoint.
            fields: Validated text form fields.
            parts: Ordered, bounded Evidence parts approved for upload.
            idempotency_key: Required stable key for the logical Judge request.
            client_request_id: Optional v2 request-recovery identity.
            _deadline: Internal monotonic operation deadline.
            _expected_status: Internal exact-success status constraint.

        Returns:
            Validated public JSON response mapping.

        Raises:
            ValidationError: Multipart metadata cannot be serialized safely.
            KumaError: Request validation, transport, status, or response fails.

        Preconditions:
            Evidence projection has enforced the dynamic file/byte limits,
            association contract, and sensitive-data policy.

        Postconditions:
            The multipart body is generated once; every retry reuses the exact
            body, boundary, and idempotency key.

        Side Effects:
            Performs bounded public Backend upload through :meth:`_send`.

        Security/Privacy:
            This boundary never contacts Core directly and must receive only the
            already allowlisted public Evidence representation.
        """
        content_type, body = encode_multipart(fields, parts)
        return self._send(
            "POST",
            path,
            content_type=content_type,
            body=body,
            idempotency_key=idempotency_key,
            client_request_id=client_request_id,
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
    "validate_client_request_id",
]
