"""Bounded polling and minimal local resume state for public v2 operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import (
    DEFAULT_CASE_MAX_STEPS,
    DEFAULT_OPERATION_WAIT_TIMEOUT,
    validate_operation_wait_timeout,
)
from ..errors import (
    KumaError,
    KumaTimeoutError,
    ProviderError,
    ServiceBusyError,
)
from .backend import BackendClient, mapped_error

_DEFAULT_RESUME_POLL_MS = 1_000
_MIN_POLL_MS = 100
_MAX_POLL_MS = 60_000
_MAX_OPERATION_ID_CHARS = 64
_STATE_SCHEMA = "defuzex.pending_operation.v1"
_STATE_LOCK = threading.RLock()


def request_identity(payload: Mapping[str, Any], *, base_url: str) -> str:
    """Return a non-reversible identity without persisting request content."""

    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "The operation request could not be identified",
            code="operation_state_invalid",
        ) from exc
    digest = hashlib.sha256()
    digest.update(base_url.encode("utf-8"))
    digest.update(b"\0")
    digest.update(serialized)
    return digest.hexdigest()


def _base_url_identity(base_url: str) -> str:
    """Hash the normalized Backend origin without persisting credentials."""
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """Persist only safe metadata required to resume an asynchronous operation.

    Attributes:
        operation_type: Closed local type, currently Case generation or Judge.
        idempotency_key: Stable public request key reused after response loss.
        base_url_identity: Hash of the public Backend identity, never its key.
        created_at: Local wall-clock seconds when this request identity began.
        updated_at: Local wall-clock seconds of the last metadata change.
        operation_id: Opaque Backend operation reference after acceptance, or
            ``None`` while retrying a lost POST response.

    Security/Privacy:
        No API key, request payload, Case, Evidence, model output, or remote error
        body is stored in this object.
    """

    operation_type: str
    idempotency_key: str
    base_url_identity: str
    created_at: float
    updated_at: float
    operation_id: str | None = None


class PendingOperationStore:
    """Atomically persist only the metadata needed to resume one operation."""

    def __init__(
        self,
        path: Path | None,
        *,
        operation_type: str,
        base_url: str,
    ) -> None:
        """Bind resumable metadata to one operation type and Backend identity.

        Args:
            path: Run-owned state file, or ``None`` for process-local state.
            operation_type: Closed Case/Judge operation label validated on load.
            base_url: Public Backend identity; only its hash is persisted.

        Raises:
            ProviderError: If the selected state path cannot be resolved safely.

        Security/Privacy:
            Construction stores no API key, request payload, or raw Backend URL
            in the persisted state.
        """
        path_failed = False
        try:
            self.path = None if path is None else path.expanduser().resolve()
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            path_failed = True
        if path_failed:
            raise ProviderError(
                "The pending operation state is unavailable",
                code="operation_state_unavailable",
            ) from None
        self.operation_type = operation_type
        self.base_url_identity = _base_url_identity(base_url)
        self._memory_state: PendingOperation | None = None

    def load(self) -> PendingOperation | None:
        """Load and validate pending state without exposing request content.

        Returns:
            Valid pending metadata, or ``None`` when no operation is stored.

        Raises:
            ProviderError: With ``operation_state_invalid`` when metadata cannot
                be inspected, decoded, or validated; raw local errors are detached.

        Side Effects:
            Reads at most the configured local state file and performs no network
            request or operation replay.
        """
        with _STATE_LOCK:
            if self.path is None:
                return self._memory_state
            try:
                if not self.path.exists():
                    return None
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
            else:
                return self._validate(raw)
            raise ProviderError(
                "The pending operation state is unreadable",
                code="operation_state_invalid",
            ) from None

    def load_or_create(self, key_factory: Callable[[], str]) -> PendingOperation:
        """Load pending state or atomically create it with one stable key."""
        with _STATE_LOCK:
            existing = self.load()
            if existing is not None:
                return existing
            now = time.time()
            state = PendingOperation(
                operation_type=self.operation_type,
                idempotency_key=key_factory(),
                base_url_identity=self.base_url_identity,
                created_at=now,
                updated_at=now,
            )
            self._write(state)
            return state

    def set_operation_id(
        self, state: PendingOperation, operation_id: str
    ) -> PendingOperation:
        """Persist the accepted operation ID without changing its stable key."""
        with _STATE_LOCK:
            validated_id = _operation_id(operation_id)
            current = self.load()
            if (
                current is not None
                and current.operation_id == validated_id
                and current.idempotency_key == state.idempotency_key
                and current.created_at == state.created_at
            ):
                return current
            if current != state:
                raise ProviderError(
                    "The pending operation state changed unexpectedly",
                    code="operation_state_conflict",
                )
            updated = replace(
                state,
                operation_id=validated_id,
                updated_at=time.time(),
            )
            self._write(updated)
            return updated

    def clear(self) -> None:
        """Remove terminal operation state while tolerating prior absence.

        Raises:
            ProviderError: If the state file cannot be removed; raw path and OS
                diagnostics are detached from the public exception chain.

        Postconditions:
            Memory state is cleared first. Successful file removal may also prune
            its now-empty Run directory without touching committed Evidence.
        """
        with _STATE_LOCK:
            self._memory_state = None
            if self.path is None:
                return
            try:
                self.path.unlink(missing_ok=True)
            except FileNotFoundError:
                return
            except OSError:
                pass
            else:
                # A non-empty Run directory can also contain committed Evidence.
                with suppress(OSError):
                    self.path.parent.rmdir()
                return
            raise ProviderError(
                "The completed operation state could not be cleared",
                code="operation_state_unavailable",
            ) from None

    def _write(self, state: PendingOperation) -> None:
        """Atomically persist non-secret resume metadata with owner-only permissions.

        Args:
            state: Validated operation ID/key/timestamp metadata; never payload or
                credential content.

        Raises:
            ProviderError: If a local filesystem operation fails. Its public code
                is ``operation_state_unavailable`` with no raw exception chain.
            BaseException: Non-filesystem serialization/control-flow errors after
                descriptor and temporary-file cleanup.

        Postconditions:
            Success atomically replaces the state file. Failure closes any raw
            descriptor still owned here and removes the temporary file.
        """
        if self.path is None:
            self._memory_state = state
            return
        payload = {
            "schema_version": _STATE_SCHEMA,
            "operation_type": state.operation_type,
            "operation_id": state.operation_id,
            "idempotency_key": state.idempotency_key,
            "base_url_identity": state.base_url_identity,
            "created_at": state.created_at,
            "updated_at": state.updated_at,
        }
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}-",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor = None
                json.dump(payload, handle, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
            return
        except BaseException as exc:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            if not isinstance(exc, OSError):
                raise
        raise ProviderError(
            "The pending operation state could not be saved",
            code="operation_state_unavailable",
        ) from None

    def _validate(self, raw: Any) -> PendingOperation:
        """Validate closed-schema state against the expected operation and Backend."""
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "operation_type",
            "operation_id",
            "idempotency_key",
            "base_url_identity",
            "created_at",
            "updated_at",
        }:
            raise ProviderError(
                "The pending operation state is invalid",
                code="operation_state_invalid",
            )
        operation_id = raw["operation_id"]
        values_valid = (
            raw["schema_version"] == _STATE_SCHEMA
            and raw["operation_type"] == self.operation_type
            and raw["base_url_identity"] == self.base_url_identity
            and isinstance(raw["idempotency_key"], str)
            and 1 <= len(raw["idempotency_key"]) <= 255
            and all(33 <= ord(char) <= 126 for char in raw["idempotency_key"])
            and (
                operation_id is None
                or (
                    isinstance(operation_id, str)
                    and 1 <= len(operation_id) <= _MAX_OPERATION_ID_CHARS
                )
            )
            and _valid_timestamp(raw["created_at"])
            and _valid_timestamp(raw["updated_at"])
            and raw["updated_at"] >= raw["created_at"]
        )
        if not values_valid:
            raise ProviderError(
                "The pending operation state is invalid",
                code="operation_state_invalid",
            )
        return PendingOperation(
            operation_type=self.operation_type,
            operation_id=operation_id,
            idempotency_key=raw["idempotency_key"],
            base_url_identity=self.base_url_identity,
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
        )


def _valid_timestamp(value: Any) -> bool:
    """Return whether timestamp satisfies the resumable asynchronous operations contract."""
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _operation_id(value: Any) -> str:
    """Validate and return a bounded opaque Backend operation identifier."""
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_OPERATION_ID_CHARS:
        raise ProviderError(
            "The Backend returned an invalid operation_id",
            code="invalid_response",
        )
    return value


def _start_response(response: Mapping[str, Any]) -> tuple[str, int]:
    """Validate the closed 202 operation envelope and bounded poll interval."""
    if set(response) != {"operation_id", "status", "poll_after_ms"}:
        raise ProviderError(
            "The Backend returned an invalid operation start response",
            code="invalid_response",
        )
    poll_after_ms = response["poll_after_ms"]
    if (
        response["status"] not in {"queued", "running", "succeeded", "failed"}
        or isinstance(poll_after_ms, bool)
        or not isinstance(poll_after_ms, int)
        or not _MIN_POLL_MS <= poll_after_ms <= _MAX_POLL_MS
    ):
        raise ProviderError(
            "The Backend returned an invalid operation start response",
            code="invalid_response",
        )
    return _operation_id(response["operation_id"]), poll_after_ms


def _poll_response(
    response: Mapping[str, Any], expected_operation_id: str
) -> tuple[
    str,
    Mapping[str, Any] | None,
    tuple[str, bool, str | None, Mapping[str, Any] | None] | None,
]:
    """Validate a poll envelope and separate active, success, and failure data.

    Args:
        response: Untrusted JSON mapping returned by the public operation GET.
        expected_operation_id: Opaque ID stored for the current resumable request.

    Returns:
        Status plus either a succeeded result mapping or a closed failed-error
        tuple. Case step-limit errors may additionally carry the sole safe detail
        ``max_allowed_steps``.

    Raises:
        ProviderError: If IDs differ, status/fields violate the closed union, or
            a field has the wrong type, range, or nested shape.

    Postconditions:
        Returned values contain no unknown response fields. This function does
        not clear pending state; ``await_operation`` commits that transition.

    Security/Privacy:
        Arbitrary remote details and malformed messages cannot enter public SDK
        exceptions through the asynchronous polling path.
    """
    operation_id = _operation_id(response.get("operation_id"))
    status = response.get("status")
    if operation_id != expected_operation_id:
        raise ProviderError(
            "The Backend returned a mismatched operation_id",
            code="invalid_response",
        )
    if status in {"queued", "running"} and set(response) == {
        "operation_id",
        "status",
    }:
        return status, None, None
    if status == "succeeded" and set(response) == {
        "operation_id",
        "status",
        "result",
    }:
        result = response["result"]
        if isinstance(result, Mapping):
            return status, result, None
    if status == "failed" and set(response) == {
        "operation_id",
        "status",
        "error",
    }:
        error = response["error"]
        if isinstance(error, Mapping) and set(error) in (
            {"code", "retryable"},
            {"code", "message", "retryable"},
            {"code", "retryable", "details"},
            {"code", "message", "retryable", "details"},
        ):
            code = error["code"]
            retryable = error["retryable"]
            message = error.get("message")
            details = error.get("details")
            details_valid = "details" not in error or (
                code == "case_step_limit_exceeded"
                and isinstance(details, Mapping)
                and set(details) == {"max_allowed_steps"}
                and not isinstance(details.get("max_allowed_steps"), bool)
                and isinstance(details.get("max_allowed_steps"), int)
                and 1 <= details["max_allowed_steps"] <= DEFAULT_CASE_MAX_STEPS
            )
            if (
                isinstance(code, str)
                and 1 <= len(code) <= 64
                and isinstance(retryable, bool)
                and ("message" not in error or isinstance(message, str))
                and details_valid
            ):
                return status, None, (code, retryable, message, details)
    raise ProviderError(
        "The Backend returned an invalid operation status response",
        code="invalid_response",
    )


StartOperation = Callable[[str, float], Mapping[str, Any]]


def await_operation(
    client: BackendClient,
    store: PendingOperationStore,
    *,
    key_factory: Callable[[], str],
    start: StartOperation,
    wait_timeout: float,
    accept_result: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Start or resume one operation until a validated terminal result arrives.

    The stable key and operation ID survive retryable transport failures and
    wait timeouts. State is cleared only after result acceptance or a terminal
    public failure, preventing a retry from creating a second paid operation.

    Args:
        client: Public Backend client used for operation-status GET requests.
        store: Resume store scoped to the exact request identity and Backend.
        key_factory: Called only when no pending state exists to create one stable
            idempotency key.
        start: POST callback receiving the stable key and absolute monotonic
            deadline. Retries must reuse its key.
        wait_timeout: Total positive seconds for start and all polling attempts.
        accept_result: Optional boundary validator/normalizer called on succeeded
            ``result`` before pending state is cleared.

    Returns:
        Terminal succeeded result, or ``accept_result(result)`` when supplied.

    Raises:
        KumaTimeoutError: Deadline expires; pending metadata remains for retry.
        KumaError: Public transport or terminal operation failure. Retryable
            interruptions preserve state except stable operation-not-found.
        ProviderError: Start/poll/result schema or local state is invalid.

    Preconditions:
        ``store`` is bound to this operation type/request identity; ``start`` is
        idempotent for the provided key.

    Postconditions:
        Success clears state only after accepted result validation. Terminal
        failed operation clears state after mapping. Timeout/transient failure
        keeps the same key and operation ID for GET-only resume where possible.

    Side Effects:
        May atomically write/delete pending metadata, issue one idempotent POST,
        sleep according to bounded server polling guidance, and issue status GETs.

    Security/Privacy:
        The store contains no credential, request payload, Evidence, or response
        body; errors expose only stable safe public fields.
    """

    deadline = time.monotonic() + validate_operation_wait_timeout(wait_timeout)
    state = store.load_or_create(key_factory)
    poll_after_ms = _DEFAULT_RESUME_POLL_MS
    if state.operation_id is None:
        response = start(state.idempotency_key, deadline)
        operation_id, poll_after_ms = _start_response(response)
        state = store.set_operation_id(state, operation_id)
    while True:
        _ensure_time_remaining(deadline)
        try:
            if isinstance(client, BackendClient):
                response = client.json(
                    "GET",
                    f"/sdk/v2/operations/{state.operation_id}/",
                    _deadline=deadline,
                    _expected_status=200,
                )
            else:
                response = client.json(
                    "GET",
                    f"/sdk/v2/operations/{state.operation_id}/",
                )
        except KumaError as exc:
            if exc.code == "operation_not_found":
                store.clear()
                raise
            if not exc.retryable or isinstance(exc, ServiceBusyError):
                raise
            _sleep_bounded(poll_after_ms, deadline)
            continue
        status, result, error = _poll_response(response, state.operation_id or "")
        if status == "succeeded" and result is not None:
            accepted = result if accept_result is None else accept_result(result)
            store.clear()
            return accepted
        if status == "failed" and error is not None:
            store.clear()
            raise mapped_error(
                error[0],
                retryable=error[1],
                message=error[2],
                details=error[3],
            )
        _sleep_bounded(poll_after_ms, deadline)


def _ensure_time_remaining(deadline: float) -> None:
    """Raise a retryable wait timeout without clearing resumable operation state."""
    if time.monotonic() >= deadline:
        raise KumaTimeoutError(
            "The KUMA operation did not finish before the wait timeout.",
            code="operation_wait_timeout",
            retryable=True,
        )


def _sleep_bounded(poll_after_ms: int, deadline: float) -> None:
    """Sleep for the server interval without crossing the operation deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _ensure_time_remaining(deadline)
    time.sleep(min(poll_after_ms / 1_000, remaining))


__all__ = [
    "DEFAULT_OPERATION_WAIT_TIMEOUT",
    "PendingOperationStore",
    "await_operation",
    "request_identity",
    "validate_operation_wait_timeout",
]
