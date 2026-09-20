"""Bounded local queries and rebinding for durable request records."""

from __future__ import annotations

from pathlib import Path

from ..errors import ProviderError
from ._request_contract import (
    RequestRecord,
    StoredRequest,
    backend_identity,
    read_record,
)
from ._request_files import canonical_repo_root, record_paths, validate_existing_ledger
from ._request_store import RequestOperationStore
from .backend import validate_client_request_id


def list_request_records(repo_path: Path) -> tuple[RequestRecord, ...]:
    """Return bounded local summaries newest-first without network access."""
    root = canonical_repo_root(repo_path)
    directory = root / ".kuma" / "requests"
    if not directory.exists():
        return ()
    validate_existing_ledger(root, directory)
    records = tuple(read_record(path).public for path in record_paths(directory))
    return tuple(
        sorted(
            records,
            key=lambda item: (item.updated_at, item.client_request_id),
            reverse=True,
        )
    )


def load_request_record(repo_path: Path, client_request_id: str) -> StoredRequest:
    """Load one exact local record after repository and ID validation."""
    root = canonical_repo_root(repo_path)
    directory = root / ".kuma" / "requests"
    validate_existing_ledger(root, directory)
    identifier = validate_client_request_id(client_request_id)
    return read_record(directory / f"{identifier}.json")


def find_active_request(
    repo_path: Path,
    *,
    request_type: str,
    run_id: str,
    base_url: str,
    api_key_sha256: str,
    include_succeeded: bool = False,
) -> StoredRequest | None:
    """Find an owner-bound Run request without reconstructing its body.

    Judge may include a retained success to recover its negotiated schema before
    rebuilding and comparing the exact request hash. Other callers retain active-
    only behavior. Reads bounded local metadata; no response/body is inferred.
    """
    root = canonical_repo_root(repo_path)
    directory = root / ".kuma" / "requests"
    if not directory.exists():
        return None
    validate_existing_ledger(root, directory)
    expected_backend = backend_identity(base_url)
    matches = [
        stored
        for stored in (read_record(path) for path in record_paths(directory))
        if stored.public.request_type == request_type
        and stored.public.run_id == run_id
        and stored.backend_sha256 == expected_backend
        and stored.api_key_sha256 == api_key_sha256
        and (
            stored.public.status not in {"succeeded", "failed"}
            or (include_succeeded and stored.public.status == "succeeded")
        )
    ]
    if len(matches) > 1:
        raise ProviderError(
            "The request ledger contains conflicting active records",
            code="operation_state_conflict",
        )
    return matches[0] if matches else None


def store_for_existing(
    repo_path: Path,
    stored: StoredRequest,
    *,
    base_url: str,
    api_key_sha256: str,
) -> RequestOperationStore:
    """Rebind public resume to the exact Backend and creating credential hash."""
    if stored.backend_sha256 != backend_identity(base_url) or (
        stored.api_key_sha256 != api_key_sha256
    ):
        raise ProviderError(
            "The request is unavailable for this Backend credential",
            code="request_not_found",
        )
    store = RequestOperationStore(
        repo_path,
        request_type=stored.public.request_type,
        request_sha256=stored.request_sha256,
        base_url=base_url,
        api_key_sha256=api_key_sha256,
        run_id=stored.public.run_id,
        case_id=stored.public.case_id,
        case_validation=stored.case_validation,
        assessment_contract=stored.assessment_contract,
    )
    store._client_request_id = stored.public.client_request_id
    return store
