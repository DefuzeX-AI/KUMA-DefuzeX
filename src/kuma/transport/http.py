"""Internal validation and timing policy for public Backend requests."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ConfigurationError, KumaTimeoutError, ServiceError

_RETRY_BACKOFF_BASE_SECONDS = 0.1
_RETRY_BACKOFF_MAX_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class WireResponse:
    """Return a decoded public HTTP status and JSON object from a transport.

    Attributes:
        status: Integer HTTP status code.
        payload: Decoded JSON object; scalar/list bodies are rejected earlier.
    """

    status: int
    payload: Mapping[str, Any]


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
    """Validate response status, body size, JSON shape, and expected status."""
    if isinstance(response, WireResponse):
        if expected_status is not None and response.status != expected_status:
            raise ServiceError(
                "The KUMA service returned an unexpected HTTP status.",
                code="invalid_response",
            )
        return response.payload
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
