"""Status-code-oriented exceptions for HTTP client consumers.

New code should import the stable v4 hierarchy from :mod:`kuma.errors`.
"""

from __future__ import annotations

from typing import Any

from .errors import (
    AuthenticationError,
    KumaError,
    KumaTimeoutError,
    ServiceError,
)


class KumaAPIError(ServiceError):
    """The KUMA API returned an error response."""

    def __init__(self, status_code: int, message: str, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"KUMA API error {status_code}: {message}",
            code="service_error",
            retryable=status_code in {408, 429, 502, 503, 504},
        )


class KumaAuthenticationError(KumaAPIError, AuthenticationError):
    """The API key is missing, invalid, expired, or revoked."""


class KumaPermissionError(KumaAPIError):
    """The API key or subscription does not permit the operation."""


class KumaRateLimitError(KumaAPIError):
    """The account has exhausted its current KUMA quota."""


__all__ = [
    "KumaAPIError",
    "KumaAuthenticationError",
    "KumaError",
    "KumaPermissionError",
    "KumaRateLimitError",
    "KumaTimeoutError",
]
