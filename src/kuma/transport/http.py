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


def safe_retry_after(value: Any) -> int | None:
    """Accept only integer delay-seconds in the frozen auth hint range 1..60.

    Args:
        value: Untrusted header text or internal integer metadata. Dates,
            duplicates, whitespace, non-ASCII digits and oversized values fail.

    Returns:
        Bounded seconds, or None to retain local backoff. No raw text is kept.
    """
    if type(value) is str and re.fullmatch(r"[1-9][0-9]?", value):
        value = int(value)
    return value if type(value) is int and 1 <= value <= 60 else None


def header_retry_after(headers: Any) -> int | None:
    """Read one bounded Retry-After header for the auth transport retry owner.

    Args:
        headers: HTTP response headers, or None. Duplicate occurrences are
            ambiguous and ignored, including comma-joined representations.

    Returns:
        Validated delay-seconds only; HTTP dates are outside this contract.
        Parsing neither sleeps nor changes retry eligibility.
    """
    if headers is None:
        return None
    values = [value for name, value in headers.items() if name.lower() == "retry-after"]
    return safe_retry_after(values[0]) if len(values) == 1 else None


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


def safe_support_code(value: Any) -> str | None:
    """Return a closed opaque support code or None, never infer a failure cause.

    Args:
        value: Untrusted response metadata; only SB000 through SB003 are valid.

    Returns:
        The unchanged allowed string, or None for all other types/spellings.
        Transport and official error translation share this pure safety filter.
    """
    return (
        value
        if isinstance(value, str) and value in {"SB000", "SB001", "SB002", "SB003"}
        else None
    )


def header_support_code(headers: Any) -> str | None:
    """Extract exactly one X-KUMA-Support-Code without retaining raw headers.

    Args:
        headers: HTTPMessage/mapping from a response, or None.

    Returns:
        A validated opaque code; absent, duplicate or invalid values yield None.
        No JSON fields or local request identities are used.
    """
    if headers is None:
        return None
    values = [
        value
        for name, value in headers.items()
        if name.lower() == "x-kuma-support-code"
    ]
    return safe_support_code(values[0]) if len(values) == 1 else None


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

    def __init__(
        self,
        payload: Mapping[str, Any],
        request_id: str | None,
        support_code: str | None = None,
    ) -> None:
        """Copy decoded payload keys and attach only sanitized HTTP metadata.

        Args:
            payload: Public JSON object returned by BackendClient's transport.
            request_id: Response header ID; never taken from a JSON body field.
            support_code: Optional closed support-code header, outside JSON.

        Postconditions:
            Mapping iteration/serialization and closed-schema checks see only
            the original keys. Concurrent responses cannot overwrite this ID.
        """
        super().__init__(payload)
        self._request_id = safe_request_id(request_id)
        self._support_code = safe_support_code(support_code)


def response_request_id(response: Mapping[str, Any]) -> str | None:
    """Return HTTP metadata from an SDK response, never a response JSON key."""
    return response._request_id if type(response) is _ResponsePayload else None


def response_support_code(response: Mapping[str, Any]) -> str | None:
    """Return only SDK response-local support metadata for terminal poll mapping.

    Args:
        response: Validated Backend mapping consumed by the operation poller.

    Returns:
        Validated metadata for an SDK response, otherwise None. Plain mappings,
        including JSON support_code claims, are never trusted. The official
        error mapper decides whether the failure is busy before exposing it.
    """
    return response._support_code if type(response) is _ResponsePayload else None


@dataclass(frozen=True, slots=True)
class WireResponse:
    """Return a decoded public HTTP status and JSON object from a transport.

    Attributes:
        status: Integer HTTP status code.
        payload: Decoded JSON object; scalar/list bodies are rejected earlier.
        request_id: Optional safe X-Request-ID from this response, not the
            client_request_id used for operation recovery.
        support_code: Optional opaque support header, validated before use.
    """

    status: int
    payload: Mapping[str, Any]
    request_id: str | None = None
    support_code: str | None = None


def validate_request(method: str, path: str, idempotency_key: str | None) -> str:
    """Restrict public routes/methods before transport and require POST identity.

    Observation listing alone permits a bounded canonical limit/cursor query;
    DELETE is restricted to one exact opaque observation path. Other methods,
    arbitrary queries, traversal and fragments retain the existing rejection.
    """
    normalized_method = method.upper()
    observation_page = (
        normalized_method == "GET"
        and re.fullmatch(
            r"/sdk/observations/\?limit=(?:[1-9]|[1-9][0-9]|100)(?:&cursor=obs_[0-9a-f]{32})?",
            path,
        )
        is not None
    )
    if (
        not path.startswith("/sdk/")
        or path.startswith("//")
        or ("?" in path and not observation_page)
        or "#" in path
        or ".." in path.split("/")
    ):
        raise ConfigurationError("Backend paths must stay under the public /sdk/ API")
    observation_delete = (
        normalized_method == "DELETE"
        and re.fullmatch(r"/sdk/observations/obs_[0-9a-f]{32}/", path) is not None
    )
    if normalized_method not in {"GET", "POST"} and not observation_delete:
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


def retry_delay(
    attempts: int, deadline: float | None, retry_after: int | None = None
) -> float:
    """Bound an eligible retry by local backoff, an auth hint and its deadline.

    Args:
        attempts: Number of retries already used by the HTTP owner.
        deadline: Original monotonic operation deadline, or None.
        retry_after: Optional validated auth delay in seconds (1..60). Invalid
            hints are ignored; a valid hint is a floor before deadline clipping.

    Returns:
        Nonnegative sleep seconds. This helper never extends attempt budgets or
        deadlines; callers must check the deadline again before network I/O.
    """
    delay = min(
        _RETRY_BACKOFF_BASE_SECONDS * (2**attempts),
        _RETRY_BACKOFF_MAX_SECONDS,
    )
    hint = safe_retry_after(retry_after)
    if hint is not None:
        delay = max(delay, hint)
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
        The original public mapping when no safe metadata exists; otherwise a detached
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
        support_code = safe_support_code(response.support_code)
        return (
            _ResponsePayload(response.payload, request_id, support_code)
            if request_id is not None or support_code is not None
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
