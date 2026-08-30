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

from ..config import DEFAULT_OPERATION_WAIT_TIMEOUT, validate_operation_wait_timeout
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
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingOperation:
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
        self.path = None if path is None else path.expanduser().resolve()
        self.operation_type = operation_type
        self.base_url_identity = _base_url_identity(base_url)
        self._memory_state: PendingOperation | None = None

    def load(self) -> PendingOperation | None:
        with _STATE_LOCK:
            if self.path is None:
                return self._memory_state
            if not self.path.exists():
                return None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ProviderError(
                    "The pending operation state is unreadable",
                    code="operation_state_invalid",
                ) from exc
            return self._validate(raw)

    def load_or_create(self, key_factory: Callable[[], str]) -> PendingOperation:
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
        with _STATE_LOCK:
            self._memory_state = None
            if self.path is None:
                return
            try:
                self.path.unlink(missing_ok=True)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ProviderError(
                    "The completed operation state could not be cleared",
                    code="operation_state_unavailable",
                ) from exc
            # A non-empty Run directory can also contain committed Evidence.
            with suppress(OSError):
                self.path.parent.rmdir()

    def _write(self, state: PendingOperation) -> None:
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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}-",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
        except OSError as exc:
            raise ProviderError(
                "The pending operation state could not be saved",
                code="operation_state_unavailable",
            ) from exc
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(payload, handle, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except BaseException as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ProviderError(
                    "The pending operation state could not be saved",
                    code="operation_state_unavailable",
                ) from exc
            raise

    def _validate(self, raw: Any) -> PendingOperation:
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
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )


def _operation_id(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_OPERATION_ID_CHARS:
        raise ProviderError(
            "The Backend returned an invalid operation_id",
            code="invalid_response",
        )
    return value


def _start_response(response: Mapping[str, Any]) -> tuple[str, int]:
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
) -> tuple[str, Mapping[str, Any] | None, tuple[str, bool] | None]:
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
        if isinstance(error, Mapping) and set(error) == {"code", "retryable"}:
            code = error["code"]
            retryable = error["retryable"]
            if (
                isinstance(code, str)
                and 1 <= len(code) <= 64
                and isinstance(retryable, bool)
            ):
                return status, None, (code, retryable)
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
    """Return one accepted public result while preserving resumable state."""

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
            raise mapped_error(error[0], retryable=error[1])
        _sleep_bounded(poll_after_ms, deadline)


def _ensure_time_remaining(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise KumaTimeoutError(
            "The KUMA operation did not finish before the wait timeout.",
            code="operation_wait_timeout",
            retryable=True,
        )


def _sleep_bounded(poll_after_ms: int, deadline: float) -> None:
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
