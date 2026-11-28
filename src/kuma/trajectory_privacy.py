"""Explicit per-operation trajectory privacy; never a global Case/Run override."""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .errors import ValidationError

_WARNING_ACTIVE = ContextVar("kuma_raw_warning_active", default=False)


class RawContentWarning(UserWarning):
    """Warn about explicitly retained original content without exposing its values."""


def checked_policy(value: bool) -> bool:
    """Reject coercion before an operation can retain, persist or transmit raw data."""
    if type(value) is not bool:
        raise ValidationError("redact_sensitive must be a boolean")
    return value


def privacy_operation(destination: str):
    """Warn once around an outer synchronous operation; never propagate permission.

    The context variable suppresses duplicate nested warnings only. Every callee
    still receives and validates its own explicit policy argument. Reset happens
    on every return or exception, including warnings promoted to errors.
    """

    def decorate(function):
        """Preserve the public callable signature and documentation."""

        @wraps(function)
        def invoke(*args, **kwargs):
            """Validate keyword-only policy and warn before invoking real work."""
            policy = checked_policy(kwargs.get("redact_sensitive", True))
            token = None
            if not policy and not _WARNING_ACTIVE.get():
                warnings.warn(
                    "Sensitive-data redaction is disabled for this operation. "
                    + destination
                    + " Logs and errors remain sanitized; previously redacted data cannot be restored.",
                    RawContentWarning,
                    stacklevel=2,
                )
                token = _WARNING_ACTIVE.set(True)
            try:
                return function(*args, **kwargs)
            finally:
                if token is not None:
                    _WARNING_ACTIVE.reset(token)

        return invoke

    return decorate


LOCAL_WARNING = "Original content is retained in the returned local data; saving it creates plaintext."
REMOTE_WARNING = "Original content may be stored in private Core and sent to the configured model provider."


@contextmanager
def warned_substep():
    """Suppress duplicate warnings during a previously warned capture lifecycle substep.

    This never changes the policy supplied to validators/mappers. Use only after
    the owning session warned before retaining its initial task; reset on failure.
    """
    token = _WARNING_ACTIVE.set(True)
    try:
        yield
    finally:
        _WARNING_ACTIVE.reset(token)


def local_observation(value, *, redact_sensitive: bool = True):
    """Validate an explicitly local raw observation without widening cloud admission.

    Only this trajectory path strips the reserved local wrapper marker before
    reusing the canonical observation structure validator. Cloud checked/export
    retain their protected defaults and reject marked raw local documents.
    """
    from . import observation_contract
    from ._json_values import detach_json
    from .repository.privacy import scan_sensitive_json

    checked_policy(redact_sensitive)
    failed = False
    try:
        plain = detach_json(value)
        if "content_privacy" in plain and (
            redact_sensitive or plain.pop("content_privacy") != "raw"
        ):
            raise ValueError
        if redact_sensitive and scan_sensitive_json(plain, location="observation"):
            raise ValueError
        result = observation_contract.export(plain, _redact_sensitive=redact_sensitive)
    except Exception:
        failed = True
    if failed:
        raise ValidationError(
            "Invalid local observation or privacy policy", code="observation_invalid"
        )
    return result
