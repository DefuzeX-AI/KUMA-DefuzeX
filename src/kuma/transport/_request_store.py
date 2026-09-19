"""Transactional state machine for one repository-local official request."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..errors import LimitExceededError, ProviderError
from ._request_contract import (
    MAX_RECORD_BYTES,
    REMOTE_STATUSES,
    REQUEST_TYPES,
    RequestRecord,
    StoredRequest,
    backend_identity,
    new_client_request_id,
    optional_identifier,
    pending_operation,
    read_record,
    require_hash,
    safe_code,
    stored_payload,
    validate_case_context,
    validate_idempotency_key,
    validate_locator,
    validate_operation_id,
)
from ._request_files import (
    atomic_write,
    canonical_repo_root,
    locked_ledger,
    record_paths,
    save_public_report,
)
from .operations import PendingOperation


class RequestOperationStore:
    """Persist one request identity and asynchronous operation lifecycle.

    The store implements the state boundary consumed by ``await_operation``.
    Creation reuses an exact active request, preventing a second operation after
    response loss or process restart.
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        request_type: str,
        request_sha256: str,
        base_url: str,
        api_key_sha256: str,
        run_id: str | None = None,
        case_id: str | None = None,
        case_validation: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind validated recovery identities without writing local state.

        The stored digests bind exact request, Backend, and API key without
        saving their values. ``case_validation`` holds only bounded public
        checks required after a process-loss resume. Unsafe paths fail before
        any network operation.
        """
        self.root = canonical_repo_root(repo_path)
        if request_type not in REQUEST_TYPES:
            raise ProviderError("Request type is invalid", code="request_state_invalid")
        require_hash(request_sha256, "request_sha256")
        require_hash(api_key_sha256, "api_key_sha256")
        self.request_type = request_type
        self.request_sha256 = request_sha256
        self.backend_sha256 = backend_identity(base_url)
        self.api_key_sha256 = api_key_sha256
        self.run_id = optional_identifier(run_id, 128)
        self.case_id = optional_identifier(case_id, 128)
        self.case_validation = validate_case_context(case_validation)
        self.directory = self.root / ".kuma" / "requests"
        self._client_request_id: str | None = None
        self._accepted_case_id: str | None = None
        self._accepted_result_locator: str | None = None

    @property
    def client_request_id(self) -> str:
        """Return the prepared ID or fail if preparation has not completed."""
        if self._client_request_id is None:
            raise ProviderError(
                "The request record has not been prepared",
                code="request_state_invalid",
            )
        return self._client_request_id

    def load(self) -> PendingOperation | None:
        """Return exact active state for retry without creating local files."""
        if not self.directory.exists():
            return None
        with self._locked(create=False):
            stored = self._find_matching()
            if stored is None:
                return None
            self._client_request_id = stored.public.client_request_id
            return pending_operation(stored)

    def load_or_create(self, key_factory: Callable[[], str]) -> PendingOperation:
        """Atomically reuse state or persist preparation before the first POST.

        ``key_factory`` runs only after a locked recheck finds no matching active
        request. The resulting complete JSON record exists before return.
        """
        with self._locked(create=True):
            stored = self._find_matching()
            if stored is None:
                now = time.time()
                stored = StoredRequest(
                    public=RequestRecord(
                        new_client_request_id(),
                        self.request_type,
                        "prepared",
                        None,
                        self.run_id,
                        self.case_id,
                        None,
                        now,
                        now,
                    ),
                    idempotency_key=validate_idempotency_key(key_factory()),
                    request_sha256=self.request_sha256,
                    backend_sha256=self.backend_sha256,
                    api_key_sha256=self.api_key_sha256,
                    case_validation=self.case_validation,
                )
                self._write(stored)
            self._client_request_id = stored.public.client_request_id
            return pending_operation(stored)

    def set_operation_id(
        self, state: PendingOperation, operation_id: str
    ) -> PendingOperation:
        """Atomically bind the accepted operation ID and queued state once."""
        return self._transition(
            state, operation_id=validate_operation_id(operation_id), status="queued"
        )

    def set_status(self, state: PendingOperation, status: str) -> PendingOperation:
        """Persist one validated queued/running poll transition."""
        if status not in {"queued", "running"}:
            raise ProviderError(
                "Request status is invalid", code="request_state_invalid"
            )
        return self._transition(state, status=status)

    def mark_succeeded(
        self,
        state: PendingOperation,
        *,
        case_id: str | None = None,
        result_locator: str | None = None,
    ) -> None:
        """Retain terminal identity after the complete result is accepted."""
        self._terminal(
            state,
            status="succeeded",
            case_id=case_id or self._accepted_case_id,
            result_locator=result_locator or self._accepted_result_locator,
        )

    def stage_public_result(
        self, *, case_id: str | None = None, result_locator: str | None = None
    ) -> None:
        """Stage safe public locators until the success transition commits."""
        self._accepted_case_id = optional_identifier(case_id, 128)
        self._accepted_result_locator = validate_locator(result_locator)

    def save_public_report(self, value: Mapping[str, Any]) -> str:
        """Save a bounded normalized report without committing success status."""
        if self.run_id is None:
            raise ProviderError(
                "Judge Run correlation is missing", code="request_state_invalid"
            )
        with self._locked(create=True):
            locator = save_public_report(self.root, self.run_id, value)
        self._accepted_result_locator = locator
        return locator

    def mark_failed(
        self, state: PendingOperation, *, code: str, retryable: bool
    ) -> None:
        """Retain a terminal public code and retry flag without remote details."""
        self._terminal(
            state,
            status="failed",
            error_code=safe_code(code),
            error_retryable=bool(retryable),
        )

    def clear(self) -> None:
        """Intentionally retain terminal records for addressable recovery."""

    def public_record(self) -> RequestRecord:
        """Load the selected record as its non-secret public projection."""
        with self._locked(create=False):
            return self._selected().public

    def stored_record(self) -> StoredRequest:
        """Load the selected record for trusted SDK recovery orchestration."""
        with self._locked(create=False):
            return self._selected()

    def attach_recovered_operation(
        self, operation_id: str, status: str
    ) -> PendingOperation:
        """Bind authenticated lookup output before GET-only operation polling."""
        if status not in REMOTE_STATUSES:
            raise ProviderError("Recovery status is invalid", code="invalid_response")
        with self._locked(create=False):
            stored = self._selected()
            validated_id = validate_operation_id(operation_id)
            if stored.public.operation_id not in {None, validated_id}:
                raise ProviderError(
                    "The recovered operation does not match local state",
                    code="operation_state_conflict",
                )
            updated = replace(
                stored,
                public=replace(
                    stored.public,
                    operation_id=validated_id,
                    # Backend terminal state is not locally terminal until the
                    # ordinary operation GET returns and its result is validated.
                    status=status if status in {"queued", "running"} else "running",
                    updated_at=time.time(),
                ),
            )
            self._write(updated)
            return pending_operation(updated)

    def _transition(
        self,
        state: PendingOperation,
        *,
        operation_id: str | None = None,
        status: str | None = None,
    ) -> PendingOperation:
        """Commit one active transition after optimistic-state matching."""
        with self._locked(create=False):
            stored = self._selected()
            if pending_operation(stored) != state:
                raise ProviderError(
                    "The request record changed unexpectedly",
                    code="operation_state_conflict",
                )
            updated = replace(
                stored,
                public=replace(
                    stored.public,
                    operation_id=operation_id or stored.public.operation_id,
                    status=status or stored.public.status,
                    updated_at=time.time(),
                ),
            )
            self._write(updated)
            return pending_operation(updated)

    def _terminal(
        self,
        state: PendingOperation,
        *,
        status: str,
        case_id: str | None = None,
        result_locator: str | None = None,
        error_code: str | None = None,
        error_retryable: bool | None = None,
    ) -> None:
        """Commit one retained terminal record after exact state matching."""
        with self._locked(create=False):
            stored = self._selected()
            if pending_operation(stored) != state:
                raise ProviderError(
                    "The request record changed unexpectedly",
                    code="operation_state_conflict",
                )
            self._write(
                replace(
                    stored,
                    public=replace(
                        stored.public,
                        status=status,
                        case_id=optional_identifier(case_id, 128)
                        or stored.public.case_id,
                        result_locator=validate_locator(result_locator),
                        updated_at=time.time(),
                    ),
                    error_code=error_code,
                    error_retryable=error_retryable,
                )
            )

    def _selected(self) -> StoredRequest:
        """Read the selected record while the ledger lock is held."""
        if self._client_request_id is None:
            stored = self._find_matching()
            if stored is None:
                raise ProviderError(
                    "The request record is unavailable", code="request_not_found"
                )
            self._client_request_id = stored.public.client_request_id
            return stored
        return read_record(self.directory / f"{self._client_request_id}.json")

    def _find_matching(self) -> StoredRequest | None:
        """Reuse active identities and successful same-Run Judge operations.

        Called under the ledger lock by load/load_or_create. A successful Judge
        is immutable for its Run, Backend and credential: changed request bytes
        fail before POST; identical bytes select the original operation for GET.
        Case generation and failed-request behavior remain unchanged. No request
        body or credentials are persisted, only their existing canonical hashes.
        """
        matches = []
        for path in record_paths(self.directory):
            stored = read_record(path)
            if not (
                stored.public.request_type == self.request_type
                and stored.backend_sha256 == self.backend_sha256
                and stored.api_key_sha256 == self.api_key_sha256
            ):
                continue
            terminal = self._retained_judgment(stored)
            if stored.request_sha256 != self.request_sha256:
                if terminal:
                    raise ProviderError(
                        "This Run already has a Judgment for different Evidence",
                        code="operation_state_conflict",
                    )
                continue
            if terminal or stored.public.status not in {"succeeded", "failed"}:
                matches.append(stored)
        if len(matches) > 1:
            raise ProviderError(
                "The request ledger contains conflicting active records",
                code="operation_state_conflict",
            )
        return matches[0] if matches else None

    def _retained_judgment(self, stored: StoredRequest) -> bool:
        """Identify a successful owner-bound Judge for this exact non-null Run.

        The caller first checks request type, Backend and key hashes. Run binding
        prevents another execution from inheriting a terminal result merely
        because its Evidence happens to have the same canonical request hash.
        """
        return (
            self.request_type == "judgment"
            and self.run_id is not None
            and stored.public.run_id == self.run_id
            and stored.public.status == "succeeded"
        )

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[None]:
        """Delegate a transaction to the shared process and OS locks."""
        with locked_ledger(self.root, self.directory, create=create):
            yield

    def _write(self, stored: StoredRequest) -> None:
        """Atomically replace one bounded owner-readable request record."""
        encoded = (
            json.dumps(
                stored_payload(stored), separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            + b"\n"
        )
        if len(encoded) > MAX_RECORD_BYTES:
            raise LimitExceededError(
                "The request record exceeds its local size limit",
                code="request_state_invalid",
            )
        atomic_write(
            self.root,
            self.directory / f"{stored.public.client_request_id}.json",
            encoded,
            message="The request record could not be saved",
        )
