"""Official v2 single-operation and synchronous batch Judge Providers."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import JudgeBatchResult
from ..errors import ConfigurationError, LimitExceededError, ProviderError
from ..repository.case_artifacts import MAX_CASE_ARTIFACT_BYTES, official_original
from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_json
from ..transport.backend import (
    BackendClient,
    UploadPart,
    mapped_error,
    new_idempotency_key,
)
from ..transport.operations import PendingOperationStore, await_operation
from ..transport.request_records import (
    RequestOperationStore,
    canonical_request_sha256,
    find_active_request,
    store_for_existing,
)
from ._official_evidence_upload import JudgeUploadConfig as _JudgeConfig
from ._official_evidence_upload import evidence_upload as _evidence_upload
from ._official_evidence_upload import judge_upload_config as _judge_config
from ._official_judgment import normalize_official_judgment as _normalize_judgment
from ._official_wire import (
    plain_json,
    required_text,
)
from .base import JudgeContext
from .normalization import normalize_report, validate_custom_case_public_data

_MAX_TRACKED_RUNS = 1024


@dataclass(frozen=True, slots=True)
class _JudgeUpload:
    """Stage one validated official Judge request and stable identity.

    Attributes:
        run_id: Public Run identifier being judged.
        metadata: Safe manifest/history mapping sent as multipart metadata.
        case_id: Legacy wire reference; new uploads use case_part instead.
        local_case_id: Public Case locator retained in the local request ledger;
            independent of the multipart case_id/case_file exclusive choice.
        case_part: Serialized original official or public custom Case.
        log_parts: Bounded Evidence parts in deterministic order.
        idempotency_key: Stable key reused for retries of this exact Run history.
    """

    run_id: str
    metadata: Mapping[str, Any]
    case_id: str | None
    case_part: UploadPart | None
    log_parts: tuple[UploadPart, ...]
    idempotency_key: str
    local_case_id: str | None = None


def _client_credential_identity(client: Any) -> str:
    """Return the real key digest or a stable identity for controlled test clients."""
    value = getattr(client, "credential_identity", None)
    if isinstance(value, str) and len(value) == 64:
        return value
    return hashlib.sha256(b"kuma-controlled-provider-client").hexdigest()


def _judge_request_sha256(
    upload: _JudgeUpload,
    fields: Mapping[str, str],
    parts: Sequence[UploadPart],
) -> str:
    """Hash Judge fields and ordered part metadata without persisting Evidence.

    Args:
        upload: Validated Judge upload correlation.
        fields: Exact multipart text fields that will be sent.
        parts: Ordered upload parts; only byte lengths and SHA-256 digests enter
            the local recovery identity.

    Returns:
        Canonical request digest stable across random multipart boundaries.

    Security/Privacy:
        Neither field bodies nor Evidence bytes are returned or persisted. The
        digest cannot be used to reconstruct their content.
    """
    return canonical_request_sha256(
        {
            "run_id": upload.run_id,
            "fields": {
                name: hashlib.sha256(value.encode("utf-8")).hexdigest()
                for name, value in sorted(fields.items())
            },
            "parts": [
                {
                    "name": part.name,
                    "filename": part.filename,
                    "content_type": part.content_type,
                    "size": len(part.data),
                    "sha256": hashlib.sha256(part.data).hexdigest(),
                }
                for part in parts
            ],
        }
    )


def _custom_case_part(
    context: JudgeContext, config: _JudgeConfig, part_prefix: str
) -> tuple[UploadPart, list[Any]]:
    """Serialize one already-preflighted custom Case under dynamic limits.

    Args:
        context: Completed custom-Case Run context.
        config: Validated public Judge upload limits.
        part_prefix: Collision-safe multipart prefix used by batch uploads.

    Returns:
        Bounded custom Case multipart part and findings from a defensive rescan
        of the exact serialized public projection.

    Raises:
        LimitExceededError: If the UTF-8 Case bytes exceed the advertised
            per-file limit.
        ProviderError: If a Case field cannot be projected as public JSON.

    Preconditions:
        :func:`_preflight_custom_case_privacy` ran before any Judge config GET.

    Postconditions:
        Success returns bytes ready for the final combined Evidence limits;
        policy enforcement still occurs in ``_prepare_upload`` before POST.

    Side Effects:
        None. No filesystem or network operation occurs.

    Security/Privacy:
        The exact upload projection is scanned again at the transport boundary;
        findings retain no matched values.
    """
    custom_case = _custom_case_upload(context)
    case_bytes = json.dumps(
        custom_case,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(case_bytes) > config.max_file_bytes:
        raise LimitExceededError(
            "The custom Case exceeds the current Judge upload limit",
            code="invalid_case_file",
        )
    part = UploadPart(
        name=f"{part_prefix}case" if part_prefix else "case_file",
        filename="kuma-custom-case.json",
        content_type="application/json",
        data=case_bytes,
    )
    return part, list(scan_sensitive_json(custom_case, location="custom_case"))


def _custom_case_upload(context: JudgeContext) -> dict[str, Any]:
    """Project the exact public custom Case fields accepted by Judge upload.

    Args:
        context: Validated Judge context whose Case lacks official provenance.

    Returns:
        Detached JSON mapping later serialized as the custom Case multipart
        part. Rubrics, extensions, logs, and other private/local state are absent.

    Raises:
        ProviderError: If a public Case field is not valid detached JSON.

    Postconditions:
        The returned shape is suitable for both early privacy preflight and the
        final bounded multipart serializer, so both scan the same public facts.

    Side Effects:
        None. This function performs no filesystem read or network request.

    Security/Privacy:
        Only public Case identity, type/schema, inputs, payloads, and public
        constraints are projected. Caller-authored Rubric fields are rejected,
        never ignored or included.
    """
    validate_custom_case_public_data(context.case)
    case_id = required_text(context.case.case_id, "custom Case case_id")
    if len(case_id) > 64:
        raise ProviderError(
            "The custom Case identifier exceeds the Judge limit",
            code="invalid_custom_case",
        )
    return {
        "schema_version": "defuzex.custom_case.v1",
        "case_id": case_id,
        "input_type": context.case.input_type,
        "input_schema": plain_json(context.case.input_schema),
        "inputs": [
            {
                "input_id": item.input_id,
                "payload_type": item.payload_type,
                "payload": plain_json(item.payload),
                "public_constraints": plain_json(item.public_constraints),
            }
            for item in context.case.inputs
        ],
    }


def _preflight_custom_case_privacy(
    context: JudgeContext, *, allow_sensitive: bool
) -> None:
    """Reject sensitive custom Case upload fields before Judge config I/O.

    Args:
        context: Completed Run context about to enter the official Judge.
        allow_sensitive: Existing explicit ordinary-Evidence policy override.

    Raises:
        SensitiveDataError: If the public Case projection contains a recognized
            sensitive shape; official originals never permit policy overrides.
        ValidationError: Official Inputs or provenance differ from their original.
        ProviderError: If the custom Case cannot be projected as public JSON.

    Preconditions:
        The caller has not fetched dynamic Judge configuration or started an
        operation for this invocation.

    Postconditions:
        Success allows normal Judge preparation. Failure performs no Backend
        request and creates no operation, reservation, or usage event.

    Side Effects:
        None.

    Security/Privacy:
        Scanning uses the same exact public projection as multipart generation;
        findings expose rule IDs and a safe location, never matched values.
    """
    if _official_case_reference(context) is not None:
        return
    enforce_sensitive_policy(
        scan_sensitive_json(_custom_case_upload(context), location="custom_case"),
        allow_sensitive=allow_sensitive,
    )


def _official_case_reference(
    context: JudgeContext,
) -> tuple[str, dict[str, str]] | None:
    """Validate official provenance and return its opaque Backend reference metadata."""
    if "official_case" not in context.case.extensions:
        return None
    _, provenance = official_original(context.case)
    metadata = {
        name: provenance[name]
        for name in ("repo_fingerprint", "case_sha256", "case_signature")
    }
    return required_text(context.case.case_id, "case_id"), metadata


def _validate_batch_contexts(
    contexts: Sequence[JudgeContext], *, max_batch_items: int | None = None
) -> None:
    """Require a non-empty bounded sequence of typed Judge contexts."""
    if not isinstance(contexts, Sequence) or isinstance(
        contexts, (str, bytes, bytearray)
    ):
        raise ConfigurationError("contexts must be a sequence of JudgeContext values")
    if not contexts:
        raise LimitExceededError(
            "Batch Judge requires at least one Run", code="invalid_batch"
        )
    if max_batch_items is not None and len(contexts) > max_batch_items:
        raise LimitExceededError(
            f"Batch Judge accepts at most {max_batch_items} Runs",
            code="invalid_batch",
        )
    if any(not isinstance(context, JudgeContext) for context in contexts):
        raise ConfigurationError("Batch Judge requires JudgeContext values")


def _batch_item(upload: _JudgeUpload) -> tuple[dict[str, Any], list[UploadPart]]:
    """Project one prepared upload into batch metadata and uniquely named parts."""
    item = {
        "client_item_id": upload.run_id,
        "idempotency_key": upload.idempotency_key,
        **dict(upload.metadata),
        "log_parts": [part.name for part in upload.log_parts],
    }
    parts: list[UploadPart] = []
    if upload.case_id is not None:
        item["case_id"] = upload.case_id
    elif upload.case_part is not None:
        item["case_file_part"] = upload.case_part.name
        parts.append(upload.case_part)
    parts.extend(upload.log_parts)
    return item, parts


def _batch_result(upload: _JudgeUpload, raw: Any) -> JudgeBatchResult:
    """Validate one ordered batch item as either a Judgment or safe public error."""
    if (
        not isinstance(raw, Mapping)
        or raw.get("client_item_id") != upload.run_id
        or not isinstance(raw.get("ok"), bool)
    ):
        raise ProviderError(
            "The Backend returned an invalid Judge batch item",
            code="invalid_response",
        )
    if raw["ok"]:
        judgment = raw.get("judgment")
        if not isinstance(judgment, Mapping):
            raise ProviderError(
                "The Backend omitted a batch Judgment", code="invalid_response"
            )
        report = normalize_report(_normalize_judgment(judgment), run_id=upload.run_id)
        return JudgeBatchResult(upload.run_id, upload.run_id, report=report)
    error = raw.get("error")
    if not isinstance(error, Mapping):
        raise ProviderError(
            "The Backend omitted a batch Judge error", code="invalid_response"
        )
    code = required_text(error.get("code"), "batch error code")
    retryable = error.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ProviderError(
            "The Backend returned an invalid batch Judge error",
            code="invalid_response",
        )
    return JudgeBatchResult(
        upload.run_id,
        upload.run_id,
        error=mapped_error(code, retryable=retryable),
    )


class OfficialJudgeProvider:
    """Upload bounded public Run Evidence and resume one official Judge operation.

    Per-Run locks, stable idempotency keys, and pending metadata prevent concurrent
    or restarted calls from creating duplicate Judge operations. Private rubric
    lookup and model execution remain entirely server-owned.
    """

    def __init__(
        self,
        client: BackendClient,
        *,
        allow_sensitive: bool = False,
        operation_wait_timeout: float = 600.0,
        state_root: Path | None = None,
    ) -> None:
        """Configure bounded public Judge uploads and per-Run resumable state."""
        self.client = client
        self.allow_sensitive = allow_sensitive
        self.operation_wait_timeout = operation_wait_timeout
        self._state_root = state_root
        self._idempotency_keys: dict[str, str] = {}
        self._operation_stores: dict[str, PendingOperationStore] = {}
        self._run_locks: dict[str, threading.Lock] = {}
        self._idempotency_lock = threading.Lock()

    def _run_lock(self, run_id: str) -> threading.Lock:
        """Return a stable per-Run lock so concurrent Judge calls submit only once."""
        with self._idempotency_lock:
            lock = self._run_locks.get(run_id)
            if lock is None:
                if len(self._run_locks) >= _MAX_TRACKED_RUNS:
                    raise LimitExceededError(
                        "The Judge Provider reached its active Run limit",
                        code="client_resource_limit",
                    )
                lock = threading.Lock()
                self._run_locks[run_id] = lock
            return lock

    def _legacy_operation_store(self, run_id: str) -> PendingOperationStore:
        """Return process-local operation state when no repository root is configured."""
        with self._idempotency_lock:
            store = self._operation_stores.get(run_id)
            if store is not None:
                return store
            store = PendingOperationStore(
                None,
                operation_type="judge",
                base_url=self.client.base_url,
            )
            self._operation_stores[run_id] = store
            return store

    def _idempotency_key(self, run_id: str) -> str:
        """Reuse one Judge key per tracked Run while bounding in-memory entries."""
        with self._idempotency_lock:
            key = self._idempotency_keys.get(run_id)
            if key is None:
                if len(self._idempotency_keys) >= _MAX_TRACKED_RUNS:
                    raise LimitExceededError(
                        "The Judge Provider reached its active Run limit",
                        code="client_resource_limit",
                    )
                key = new_idempotency_key("judge")
                self._idempotency_keys[run_id] = key
            return key

    def _prepare_upload(
        self,
        context: JudgeContext,
        config: _JudgeConfig,
        *,
        part_prefix: str = "",
        idempotency_key: str | None = None,
    ) -> _JudgeUpload:
        """Build and privacy-check an official or custom Case Judge upload."""
        run_id = self._run_id(context)
        log_parts, manifest, findings = _evidence_upload(context, config, part_prefix)
        metadata: dict[str, Any] = {
            "status": self._submission_status(context),
            "force": False,
            "allow_sensitive": self.allow_sensitive,
            "manifest": manifest,
        }
        official = _official_case_reference(context)
        case_id: str | None = None
        case_part: UploadPart | None = None
        if official is not None:
            _, integrity = official
            metadata.update(integrity)
            raw, _ = official_original(context.case)
            encoded = json.dumps(
                raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(encoded) > min(MAX_CASE_ARTIFACT_BYTES, config.max_file_bytes):
                raise LimitExceededError(
                    "Official Case exceeds the upload limit", code="invalid_case_file"
                )
            case_part = UploadPart(
                name=f"{part_prefix}case" if part_prefix else "case_file",
                filename="kuma-official-case.json",
                content_type="application/json",
                data=encoded,
            )
            if (
                len(log_parts) + 1 > config.max_files
                or len(encoded) + sum(len(part.data) for part in log_parts)
                > config.max_total_bytes
            ):
                raise LimitExceededError(
                    "Case and Evidence exceed the upload limit",
                    code="log_size_exceeded",
                )
        else:
            case_part, case_findings = _custom_case_part(context, config, part_prefix)
            findings.extend(case_findings)
        enforce_sensitive_policy(findings, allow_sensitive=self.allow_sensitive)
        return _JudgeUpload(
            run_id=run_id,
            metadata=metadata,
            case_id=case_id,
            case_part=case_part,
            log_parts=log_parts,
            idempotency_key=idempotency_key or self._idempotency_key(run_id),
            local_case_id=context.case.case_id,
        )

    def judge(self, context: JudgeContext) -> Mapping[str, Any]:
        """Judge one completed Run through a serialized per-Run operation.

        Args:
            context: Immutable completed Case/history/Evidence context.

        Returns:
            Validated public Judgment mapping ready for report normalization.

        Raises:
            SensitiveDataError: If selected upload content violates policy.
            LimitExceededError: If local/dynamic upload or tracked-Run limits are
                exceeded before an unsafe request.
            ProviderError: If public configuration, operation state, or response
                is malformed.
            KumaError: For authenticated transport, terminal service failure, or
                bounded operation timeout.

        Preconditions:
            History is complete, internally correlated, and belongs to one Run.

        Postconditions:
            Concurrent calls for the same Run execute one critical section.
            Success clears pending operation state only after Judgment validation;
            retryable failure preserves identity for safe resume.

        Side Effects:
            Reads public Judge limits, may persist non-secret pending metadata,
            uploads bounded multipart Evidence, and polls the public Backend.

        Security/Privacy:
            No private rubric is read or uploaded by the SDK. Raw remote/internal
            error text is not exposed.
        """

        run_id = self._run_id(context)
        _preflight_custom_case_privacy(
            context,
            allow_sensitive=self.allow_sensitive,
        )
        with self._run_lock(run_id):
            return self._judge_locked(context, run_id)

    def _judge_locked(self, context: JudgeContext, run_id: str) -> Mapping[str, Any]:
        """Resume an accepted Judge operation or prepare and submit one new operation."""
        existing = self._active_request(run_id)
        if existing is not None:
            durable_root = self._durable_root()
            assert durable_root is not None
            store = store_for_existing(
                durable_root,
                existing,
                base_url=self.client.base_url,
                api_key_sha256=_client_credential_identity(self.client),
            )
        else:
            store = self._legacy_operation_store(run_id)
        pending = store.load()
        if pending is not None and pending.operation_id is not None:
            return self._resume_judgment(store, pending.idempotency_key)
        config = _judge_config(self.client.json("GET", "/sdk/judge/config/"))
        key = pending.idempotency_key if pending else self._idempotency_key(run_id)
        upload = self._prepare_upload(
            context,
            config,
            idempotency_key=key,
        )
        return self._submit_judgment(store, upload, existing=existing)

    def _durable_root(self) -> Path | None:
        """Return an existing repository root or select process-local fallback.

        ``create_run`` always supplies its validated repository. Direct Provider
        tests and custom integrations that omit a usable root retain the previous
        process-local behavior instead of creating an arbitrary directory.
        """
        return (
            self._state_root
            if self._state_root is not None and self._state_root.is_dir()
            else None
        )

    def _active_request(self, run_id: str) -> Any:
        """Locate a durable nonterminal Judge record after process loss.

        Returns:
            Internal stored metadata when ``state_root`` names the repository,
            otherwise ``None`` for direct process-local Provider use.

        Side Effects:
            Reads only bounded request-ledger metadata and performs no network.
        """
        durable_root = self._durable_root()
        if durable_root is None:
            return None
        return find_active_request(
            durable_root,
            request_type="judgment",
            run_id=run_id,
            base_url=self.client.base_url,
            api_key_sha256=_client_credential_identity(self.client),
        )

    def _resume_judgment(
        self, store: PendingOperationStore, idempotency_key: str
    ) -> Mapping[str, Any]:
        """Poll the stored Judge operation without issuing a replacement POST."""
        response = await_operation(
            self.client,
            store,
            key_factory=lambda: idempotency_key,
            start=lambda _key, _deadline: {},
            wait_timeout=self.operation_wait_timeout,
            accept_result=lambda value: self._accept_judgment(store, value),
        )
        return response

    def _submit_judgment(
        self,
        store: PendingOperationStore,
        upload: _JudgeUpload,
        *,
        existing: Any = None,
    ) -> Mapping[str, Any]:
        """Submit one multipart Judge operation and retain its key for safe replay."""
        fields = {"metadata": json.dumps(upload.metadata, separators=(",", ":"))}
        parts = list(upload.log_parts)
        if upload.case_id is not None:
            fields["case_id"] = upload.case_id
        elif upload.case_part is not None:
            parts.insert(0, upload.case_part)

        durable_root = self._durable_root()
        if durable_root is not None:
            request_sha256 = _judge_request_sha256(upload, fields, parts)
            if existing is not None and existing.request_sha256 != request_sha256:
                raise ProviderError(
                    "The pending Judge request no longer matches Run history",
                    code="operation_state_conflict",
                )
            if existing is None:
                store = RequestOperationStore(
                    durable_root,
                    request_type="judgment",
                    request_sha256=request_sha256,
                    base_url=self.client.base_url,
                    api_key_sha256=_client_credential_identity(self.client),
                    run_id=upload.run_id,
                    case_id=upload.local_case_id,
                )

        def start_operation(key: str, deadline: float) -> Mapping[str, Any]:
            """POST the prepared Judge upload once per stable key and deadline."""
            kwargs: dict[str, Any] = {"idempotency_key": key}
            if isinstance(store, RequestOperationStore) and isinstance(
                self.client, BackendClient
            ):
                kwargs["client_request_id"] = store.client_request_id
            if isinstance(self.client, BackendClient):
                kwargs["_deadline"] = deadline
                kwargs["_expected_status"] = 202
            return self.client.multipart(
                "/sdk/v2/judge/",
                fields,
                parts,
                **kwargs,
            )

        response = await_operation(
            self.client,
            store,
            key_factory=lambda: upload.idempotency_key,
            start=start_operation,
            wait_timeout=self.operation_wait_timeout,
            accept_result=lambda value: self._accept_judgment(store, value),
        )
        return response

    @staticmethod
    def _accept_judgment(
        store: PendingOperationStore, response: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Validate a public Judgment and save its normalized public report.

        The operation coordinator invokes this callback before committing the
        succeeded state. Durable stores therefore never advertise a report until
        both remote validation and atomic local report persistence succeed.
        """
        normalized = _normalize_judgment(response)
        if isinstance(store, RequestOperationStore):
            record = store.public_record()
            if record.run_id is None:
                raise ProviderError("Judge Run correlation is missing")
            report = normalize_report(normalized, run_id=record.run_id)
            report_payload = {
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
            store.save_public_report(plain_json(report_payload))
        return normalized

    def judge_batch(
        self, contexts: Sequence[JudgeContext]
    ) -> tuple[JudgeBatchResult, ...]:
        """Judge a bounded sequence through the legacy synchronous batch endpoint.

        Args:
            contexts: Non-empty unique-Run completed contexts, no larger than the
                Backend-advertised ``max_batch_items``.

        Returns:
            Tuple in request order. Each :class:`JudgeBatchResult` contains
            exactly one normalized report or stable public item error.

        Raises:
            ConfigurationError: If contexts are empty, duplicated, or invalid.
            LimitExceededError: If batch/upload limits are exceeded.
            SensitiveDataError: If an upload violates privacy policy.
            KumaError: If the batch transport or outer response fails.

        Postconditions:
            Item failures remain item failures and are never fabricated as
            reports; ordering is stable.

        Side Effects:
            Performs public config GET and one idempotent multipart POST. Unlike
            single Judge, batch remains synchronous v1 and stores no operation ID.
        """

        _validate_batch_contexts(contexts)
        for context in contexts:
            _preflight_custom_case_privacy(
                context,
                allow_sensitive=self.allow_sensitive,
            )
        config = _judge_config(self.client.json("GET", "/sdk/judge/config/"))
        _validate_batch_contexts(contexts, max_batch_items=config.max_batch_items)
        uploads = tuple(
            self._prepare_upload(context, config, part_prefix=f"item-{index}-")
            for index, context in enumerate(contexts)
        )
        run_ids = [upload.run_id for upload in uploads]
        if len(run_ids) != len(set(run_ids)):
            raise ConfigurationError("Batch Judge Run IDs must be unique")
        items: list[dict[str, Any]] = []
        parts: list[UploadPart] = []
        for upload in uploads:
            item, item_parts = _batch_item(upload)
            items.append(item)
            parts.extend(item_parts)
        if (
            len(parts) > config.max_files
            or sum(len(part.data) for part in parts) > config.max_total_bytes
        ):
            raise LimitExceededError(
                "Case and Evidence exceed the batch upload limit",
                code="log_size_exceeded",
            )
        response = self.client.multipart(
            "/sdk/judge/batch/",
            {"batch": json.dumps({"items": items}, separators=(",", ":"))},
            parts,
            idempotency_key=new_idempotency_key("judgebatch"),
        )
        raw_results = response.get("results")
        if not isinstance(raw_results, list) or len(raw_results) != len(uploads):
            raise ProviderError(
                "The Backend returned an invalid Judge batch", code="invalid_response"
            )
        return tuple(
            _batch_result(upload, raw)
            for upload, raw in zip(uploads, raw_results, strict=True)
        )

    @staticmethod
    def _run_id(context: JudgeContext) -> str:
        """Require non-empty history belonging to exactly one Run identifier."""
        if not context.history:
            raise ProviderError("Judge requires at least one submitted Input")
        run_id = context.history[0].submission.run_id
        if any(item.submission.run_id != run_id for item in context.history):
            raise ProviderError("Judge history contains multiple Run IDs")
        return run_id

    @staticmethod
    def _submission_status(context: JudgeContext) -> str:
        """Return the latest non-completed Submission status or overall Run status."""
        for item in reversed(context.history):
            if item.submission.status != "completed":
                return item.submission.status
        return context.run_status


__all__ = ["OfficialJudgeProvider"]
