"""Stable error types for the KUMA v4 public API.

Exception messages intentionally exclude ``details`` so diagnostic payloads cannot
accidentally disclose credentials when an exception is printed or logged.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class KumaError(Exception):
    """Base exception raised by the KUMA SDK."""

    default_code = "kuma_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Store public error attributes without adding diagnostics to the message.

        Args:
            message: Safe user-facing explanation; details are not appended.
            code: Stable programmatic category, or the subclass default if None.
            request_id: Optional response correlation ID. Official transport
                accepts only a safe X-Request-ID header; missing or invalid IDs
                remain None. For async terminal errors this identifies the poll
                response, not the original operation. Custom callers own the
                safety of values they explicitly supply here.
            retryable: Whether the same logical request may succeed on retry;
                this does not itself authorize automatic retries.
            details: Safe public fields copied into a read-only mapping; None
                means no details. Do not include credentials or raw responses.

        Postconditions:
            No ID is generated or derived from client_request_id, operation_id,
            or private Core metadata. Construction performs no I/O.
        """
        self.code = code or self.default_code
        self.request_id = request_id
        self.retryable = retryable
        self.details = MappingProxyType(dict(details or {}))
        super().__init__(message)


class ConfigurationError(KumaError):
    """Report invalid local SDK configuration before unsafe side effects occur."""

    default_code = "config_invalid"


class AuthenticationError(KumaError):
    """Report a missing, invalid, expired, or revoked public API credential."""

    default_code = "auth_invalid"


class PermissionDeniedError(KumaError):
    """Report that a valid credential lacks permission for an operation."""

    default_code = "forbidden"


class ValidationError(KumaError):
    """Report invalid caller input or malformed data at a public boundary."""

    default_code = "validation_error"


class SensitiveDataError(ValidationError):
    """Report Evidence rejected by the SDK sensitive-data policy."""

    default_code = "sensitive_data_blocked"


class DockerRequiredError(ConfigurationError):
    """Report that the requested Run requires the supported container boundary."""

    default_code = "docker_required"


class RunAlreadyActiveError(KumaError):
    """Report that another Run already owns the process/container runtime lease."""

    default_code = "run_already_active"


class ProviderError(KumaError):
    """Report a safe Case or Judge Provider failure without leaking internals."""

    default_code = "provider_failed"


class InputProtocolError(KumaError):
    """Report an invalid call for the Run's current lifecycle state."""

    default_code = "invalid_run_state"


class EvidenceCaptureError(KumaError):
    """Report Evidence capture that could not satisfy its safety contract."""

    default_code = "evidence_capture_failed"


class LimitExceededError(ValidationError):
    """Report a caller value or payload that exceeds a documented public limit."""

    default_code = "limit_exceeded"


class CaseIntegrityError(ValidationError):
    """Report official Case provenance or correlation that fails closed."""

    default_code = "invalid_case_integrity"


class RepoStateMismatchError(ValidationError):
    """Report repository state that no longer matches the validated Case context."""

    default_code = "repo_state_mismatch"


class ServiceBusyError(KumaError):
    """Report explicit upstream capacity exhaustion that callers should not retry blindly."""

    default_code = "service_busy"


class KumaTimeoutError(KumaError):
    """Report a bounded request or operation wait deadline being reached."""

    default_code = "timeout"


class ServiceError(KumaError):
    """Report a safe public service or transport failure not covered more specifically."""

    default_code = "service_error"


__all__ = [
    "AuthenticationError",
    "CaseIntegrityError",
    "ConfigurationError",
    "DockerRequiredError",
    "EvidenceCaptureError",
    "InputProtocolError",
    "KumaError",
    "KumaTimeoutError",
    "LimitExceededError",
    "PermissionDeniedError",
    "ProviderError",
    "RepoStateMismatchError",
    "RunAlreadyActiveError",
    "SensitiveDataError",
    "ServiceBusyError",
    "ServiceError",
    "ValidationError",
]
