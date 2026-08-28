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
        self.code = code or self.default_code
        self.request_id = request_id
        self.retryable = retryable
        self.details = MappingProxyType(dict(details or {}))
        super().__init__(message)


class ConfigurationError(KumaError):
    default_code = "config_invalid"


class AuthenticationError(KumaError):
    default_code = "auth_invalid"


class PermissionDeniedError(KumaError):
    default_code = "forbidden"


class ValidationError(KumaError):
    default_code = "validation_error"


class SensitiveDataError(ValidationError):
    default_code = "sensitive_data_blocked"


class DockerRequiredError(ConfigurationError):
    default_code = "docker_required"


class RunAlreadyActiveError(KumaError):
    default_code = "run_already_active"


class ProviderError(KumaError):
    default_code = "provider_failed"


class InputProtocolError(KumaError):
    default_code = "invalid_run_state"


class EvidenceCaptureError(KumaError):
    default_code = "evidence_capture_failed"


class LimitExceededError(ValidationError):
    default_code = "limit_exceeded"


class CaseIntegrityError(ValidationError):
    default_code = "invalid_case_integrity"


class RepoStateMismatchError(ValidationError):
    default_code = "repo_state_mismatch"


class ServiceBusyError(KumaError):
    default_code = "service_busy"


class KumaTimeoutError(KumaError):
    default_code = "timeout"


class ServiceError(KumaError):
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
