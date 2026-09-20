"""Public local request inspection and cross-process recovery APIs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .client import DEFAULT_BASE_URL
from .config import DEFAULT_OPERATION_WAIT_TIMEOUT
from .errors import KumaError, ProviderError
from .providers._official_judgment import normalize_official_judgment
from .providers._official_wire import plain_json
from .providers.normalization import normalize_report
from .providers.official_case import _normalized_case
from .transport.backend import BackendClient
from .transport.operations import await_operation
from .transport.request_records import (
    RequestOperationStore,
    RequestRecord,
    list_request_records,
    load_request_record,
    store_for_existing,
    validate_recovery_response,
)


def list_requests(
    repo_path: str | Path = ".",
) -> tuple[RequestRecord, ...]:
    """List addressable official requests saved for one repository.

    Args:
        repo_path: Repository whose SDK-owned ``.kuma/requests`` ledger should
            be inspected. Relative paths resolve from the caller's current
            process directory.

    Returns:
        Immutable newest-first summaries. An absent ledger returns an empty
        tuple.

    Raises:
        ConfigurationError: If ``repo_path`` is not an existing directory.
        ProviderError: If the ledger is unsafe, oversized, or malformed.

    Side Effects:
        Reads bounded local metadata only; no credential or network is used.
    """
    return list_request_records(Path(repo_path))


def show_request(
    client_request_id: str,
    *,
    repo_path: str | Path = ".",
) -> RequestRecord:
    """Return one closed, non-secret local request summary.

    Args:
        client_request_id: Exact ``kreq_`` recovery identifier printed by KUMA.
        repo_path: Repository containing the local request ledger.

    Returns:
        Public request status and optional Case/Run/report locators.

    Raises:
        ValidationError: If the identifier has the wrong public shape.
        ProviderError: If the record is missing, unsafe, or malformed.

    Side Effects:
        Reads one local JSON record and performs no network request.
    """
    return load_request_record(Path(repo_path), client_request_id).public


def resume_request(
    client_request_id: str,
    *,
    repo_path: str | Path = ".",
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    operation_wait_timeout: float = DEFAULT_OPERATION_WAIT_TIMEOUT,
    max_retries: int = 2,
) -> RequestRecord:
    """Resume one official request without reconstructing a lost ``Run``.

    Args:
        client_request_id: Exact local request handle created before the first
            official Case/Judge POST.
        repo_path: Repository containing ``.kuma/requests``.
        api_key: Exact creating API key, or ``None`` to use normal KUMA
            credential resolution.
        base_url: Public Website Backend URL used for the original request.
        timeout: Per-attempt public HTTP timeout in seconds.
        operation_wait_timeout: Total bounded lookup/poll wait in seconds.
        max_retries: Bounded transient HTTP retries per request.

    Returns:
        Updated terminal public record. Successful Judge recovery also writes a
        normalized public report and exposes its repository-relative locator.

    Raises:
        AuthenticationError: If the original credential is unavailable.
        ProviderError: If local state/wire validation fails, or a prepared record
            has no Backend operation. In the latter case ``code`` is
            ``request_not_started`` and callers must repeat the original high-
            level Case/Judge call because KUMA never persisted its body.
        KumaTimeoutError: If bounded polling expires; the same record remains
            available for another resume.
        KumaError: For safe public Backend errors.

    Preconditions:
        The same Backend and exact creating API key are supplied. KUMA does not
        store credentials and the Backend intentionally hides cross-key records.

    Postconditions:
        A known operation is only polled with GET. A prepared record first uses
        authenticated recovery lookup; a lookup 404 never triggers a bodyless
        POST or a new paid operation. Terminal identity remains on disk.

    Side Effects:
        Reads/updates bounded local metadata, may perform one recovery GET plus
        operation-status GETs, and may atomically save a public Judge report.

    Security/Privacy:
        No request, Evidence, Rubric, prompt, API key, or remote error body is
        persisted or printed. Binding uses one-way Backend/key identities.
    """
    root = Path(repo_path)
    stored = load_request_record(root, client_request_id)
    if stored.public.status in {"succeeded", "failed"}:
        return stored.public
    client = BackendClient(
        api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )
    store = store_for_existing(
        root,
        stored,
        base_url=client.base_url,
        api_key_sha256=client.credential_identity,
    )
    state = store.load()
    if state is None:
        raise ProviderError("Request record was not found", code="request_not_found")
    if state.operation_id is None:
        _recover_operation(client, store, stored.public)
    await_operation(
        client,
        store,
        key_factory=lambda: stored.idempotency_key,
        start=lambda _key, _deadline: {},
        wait_timeout=operation_wait_timeout,
        accept_result=lambda result: _accept_recovered_result(store, result),
    )
    return store.public_record()


def _recover_operation(
    client: BackendClient,
    store: RequestOperationStore,
    record: RequestRecord,
) -> None:
    """Resolve a lost POST response through the frozen authenticated lookup."""
    try:
        response = client.json("GET", f"/sdk/requests/{record.client_request_id}/")
    except KumaError as exc:
        if exc.code == "operation_not_found":
            raise ProviderError(
                "No accepted operation was found; repeat the original Case or Judge call.",
                code="request_not_started",
                retryable=True,
            ) from None
        raise
    operation_id, status = validate_recovery_response(
        response,
        client_request_id=record.client_request_id,
        request_type=record.request_type,
    )
    store.attach_recovered_operation(operation_id, status)


def _accept_recovered_result(
    store: RequestOperationStore, result: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Validate a recovered terminal result before committing local success."""
    stored = store.stored_record()
    if stored.public.request_type == "judgment":
        normalized = normalize_official_judgment(
            result,
            allow_run_receipt=True,
            run_id=stored.public.run_id,
            case_id=stored.public.case_id,
            operation_id=stored.public.operation_id,
            assessment_contract=stored.assessment_contract,
        )
        run_id = stored.public.run_id
        if run_id is None:
            raise ProviderError(
                "Judge Run correlation is missing", code="request_state_invalid"
            )
        report = normalize_report(normalized, run_id=run_id)
        store.save_public_report(
            plain_json(
                {
                    "schema_version": report.schema_version,
                    "report_id": report.report_id,
                    "run_id": report.run_id,
                    "status": report.status,
                    "confidence": report.confidence,
                    "stop_reason": report.stop_reason,
                    "issues": list(report.issues),
                    "evidence_gaps": list(report.evidence_gaps),
                    "extensions": dict(report.extensions),
                }
            )
        )
        return normalized
    context = stored.case_validation
    if context is None:
        raise ProviderError(
            "Case recovery context is missing", code="request_state_invalid"
        )
    normalized_case = _normalized_case(
        result,
        repo_fingerprint=str(context["repo_fingerprint"]),
        max_steps=int(context["max_steps"]),
        requested_strategy_id=str(context["strategy_id"]),
        requested_strategy_group=context["strategy_group_selection"],
    )
    store.stage_public_result(case_id=str(normalized_case["case_id"]))
    return normalized_case


__all__ = ["RequestRecord", "list_requests", "resume_request", "show_request"]
