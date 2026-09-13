"""Internal validation and timing policy for public Backend requests."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError, KumaTimeoutError, ServiceError

_RETRY_BACKOFF_BASE_SECONDS = 0.1
_RETRY_BACKOFF_MAX_SECONDS = 2.0


def safe_request_id(value: Any) -> str | None:
    """Accept only the Backend's default 32-lowercase-hex correlation spelling.

    Args:
        value: Untrusted response header or internal transport metadata.

    Returns:
        The unchanged ID, or None for missing, duplicate, oversized, or other
        spellings. This is a conservative SDK safety filter, not proof that the
        server generated an ID; Backend can echo a caller-supplied header.
    """
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
        else None
    )


def header_request_id(headers: Any) -> str | None:
    """Read one X-Request-ID from urllib headers without retaining other headers.

    Args:
        headers: HTTPMessage or mapping response headers, or None when absent.

    Returns:
        A safe ID only when exactly one header occurs. Duplicate headers are
        discarded rather than picking an ambiguous correlation value.
    """
    if headers is None:
        return None
    values = [
        value for name, value in headers.items() if name.lower() == "x-request-id"
    ]
    return safe_request_id(values[0]) if len(values) == 1 else None


class _ResponsePayload(dict[str, Any]):
    """Carry response-local correlation outside the closed public JSON keys."""

    def __init__(self, payload: Mapping[str, Any], request_id: str | None) -> None:
        """Copy decoded payload keys and attach only sanitized HTTP metadata.

        Args:
            payload: Public JSON object returned by BackendClient's transport.
            request_id: Response header ID; never taken from a JSON body field.

        Postconditions:
            Mapping iteration/serialization and closed-schema checks see only
            the original keys. Concurrent responses cannot overwrite this ID.
        """
        super().__init__(payload)
        self._request_id = safe_request_id(request_id)


def response_request_id(response: Mapping[str, Any]) -> str | None:
    """Return HTTP metadata from an SDK response, never a response JSON key."""
    return response._request_id if type(response) is _ResponsePayload else None


@dataclass(frozen=True, slots=True)
class WireResponse:
    """Return a decoded public HTTP status and JSON object from a transport.

    Attributes:
        status: Integer HTTP status code.
        payload: Decoded JSON object; scalar/list bodies are rejected earlier.
        request_id: Optional safe X-Request-ID from this response, not the
            client_request_id used for operation recovery.
    """

    status: int
    payload: Mapping[str, Any]
    request_id: str | None = None


def validate_request(method: str, path: str, idempotency_key: str | None) -> str:
    """Reject an oversized serialized request before network I/O."""
    normalized_method = method.upper()
    if (
        not path.startswith("/sdk/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or ".." in path.split("/")
    ):
        raise ConfigurationError("Backend paths must stay under the public /sdk/ API")
    if normalized_method not in {"GET", "POST"}:
        raise ConfigurationError("BackendClient supports GET and POST only")
    if normalized_method == "POST" and idempotency_key is None:
        raise ConfigurationError("POST requests require an idempotency_key")
    return normalized_method


def request_timeout(default: float, deadline: float | None) -> float:
    """Clamp one HTTP attempt to both its timeout and remaining deadline."""
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise KumaTimeoutError(
            "The KUMA operation wait timeout elapsed.",
            code="operation_wait_timeout",
            retryable=True,
        )
    return min(default, remaining)


def retry_delay(attempts: int, deadline: float | None) -> float:
    """Return bounded exponential backoff for a transient HTTP attempt."""
    delay = min(
        _RETRY_BACKOFF_BASE_SECONDS * (2**attempts),
        _RETRY_BACKOFF_MAX_SECONDS,
    )
    if deadline is None:
        return delay
    return min(delay, max(0.0, deadline - time.monotonic()))


def validated_response(
    response: Mapping[str, Any] | WireResponse,
    expected_status: int | None,
) -> Mapping[str, Any]:
    """Validate an HTTP result while keeping safe correlation outside JSON keys.

    Args:
        response: BackendClient transport result. WireResponse carries status and
            optional header metadata; legacy mapping transports have no header ID.
        expected_status: Required HTTP status, or None to accept the transport's
            successful response. Legacy mapping transports have no status to test.

    Returns:
        The original public mapping when no safe ID exists; otherwise a detached
        mapping with response-local metadata readable by the operation poller.
        JSON keys and serialization are unchanged.

    Raises:
        ServiceError: Status mismatch or non-mapping transport result. Status
            failures retain only a sanitized response ID, never raw headers.

    Preconditions:
        The real transport has already bounded and decoded response bytes.

    Postconditions:
        No network or retry occurs here, and no global last-response state is set.
    """
    if isinstance(response, WireResponse):
        if expected_status is not None and response.status != expected_status:
            raise ServiceError(
                "The KUMA service returned an unexpected HTTP status.",
                code="invalid_response",
                request_id=safe_request_id(response.request_id),
            )
        request_id = safe_request_id(response.request_id)
        return (
            _ResponsePayload(response.payload, request_id)
            if request_id is not None
            else response.payload
        )
    if not isinstance(response, Mapping):
        raise ServiceError(
            "The KUMA transport returned an invalid response.",
            code="invalid_response",
        )
    return response


__all__ = [
    "WireResponse",
    "request_timeout",
    "retry_delay",
    "validate_request",
    "validated_response",
]
