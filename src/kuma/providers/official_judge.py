"""Official v2 single-operation and synchronous batch Judge Providers."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import JudgeBatchResult
from ..errors import ConfigurationError, LimitExceededError, ProviderError
from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_text
from ..transport.backend import (
    BackendClient,
    UploadPart,
    mapped_error,
    new_idempotency_key,
)
from ..transport.operations import PendingOperationStore, await_operation
from ._official_evidence_upload import JudgeUploadConfig as _JudgeConfig
from ._official_evidence_upload import evidence_upload as _evidence_upload
from ._official_evidence_upload import judge_upload_config as _judge_config
from ._official_judgment import normalize_official_judgment as _normalize_judgment
from ._official_wire import (
    plain_json,
    required_text,
    validate_official_case_provenance,
)
from .base import JudgeContext
from .normalization import normalize_report

_MAX_TRACKED_RUNS = 1024


@dataclass(frozen=True, slots=True)
class _JudgeUpload:
    run_id: str
    metadata: Mapping[str, Any]
    case_id: str | None
    case_part: UploadPart | None
    log_parts: tuple[UploadPart, ...]
    idempotency_key: str


def _custom_case_part(
    context: JudgeContext, config: _JudgeConfig, part_prefix: str
) -> tuple[UploadPart, list[Any]]:
    custom_case = {
        "schema_version": "defuzex.custom_case.v1",
        "case_id": context.case.case_id,
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
    return part, list(
        scan_sensitive_text(case_bytes.decode("utf-8"), location="custom_case")
    )


def _official_case_reference(
    context: JudgeContext,
) -> tuple[str, dict[str, str]] | None:
    if "official_case" not in context.case.extensions:
        return None
    official = context.case.extensions["official_case"]
    provenance = validate_official_case_provenance(official)
    metadata = {
        name: provenance[name]
        for name in ("repo_fingerprint", "case_sha256", "case_signature")
    }
    return required_text(context.case.case_id, "case_id"), metadata


def _validate_batch_contexts(
    contexts: Sequence[JudgeContext], *, max_batch_items: int | None = None
) -> None:
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
    """Upload public Run Evidence and wait on the Website Backend v2 operation."""

    def __init__(
        self,
        client: BackendClient,
        *,
        allow_sensitive: bool = False,
        operation_wait_timeout: float = 600.0,
        state_root: Path | None = None,
    ) -> None:
        self.client = client
        self.allow_sensitive = allow_sensitive
        self.operation_wait_timeout = operation_wait_timeout
        self._state_root = state_root
        self._idempotency_keys: dict[str, str] = {}
        self._operation_stores: dict[str, PendingOperationStore] = {}
        self._run_locks: dict[str, threading.Lock] = {}
        self._idempotency_lock = threading.Lock()

    def _run_lock(self, run_id: str) -> threading.Lock:
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

    def _operation_store(self, run_id: str) -> PendingOperationStore:
        with self._idempotency_lock:
            store = self._operation_stores.get(run_id)
            if store is not None:
                return store
            path = (
                None
                if self._state_root is None
                else self._state_root / run_id / "pending-judge.json"
            )
            store = PendingOperationStore(
                path,
                operation_type="judge",
                base_url=self.client.base_url,
            )
            self._operation_stores[run_id] = store
            return store

    def _idempotency_key(self, run_id: str) -> str:
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
            case_id, integrity = official
            metadata.update(integrity)
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
        )

    def judge(self, context: JudgeContext) -> Mapping[str, Any]:
        """Judge one completed Run using a stable per-Run idempotency key."""

        run_id = self._run_id(context)
        with self._run_lock(run_id):
            return self._judge_locked(context, run_id)

    def _judge_locked(self, context: JudgeContext, run_id: str) -> Mapping[str, Any]:
        store = self._operation_store(run_id)
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
        return self._submit_judgment(store, upload)

    def _resume_judgment(
        self, store: PendingOperationStore, idempotency_key: str
    ) -> Mapping[str, Any]:
        response = await_operation(
            self.client,
            store,
            key_factory=lambda: idempotency_key,
            start=lambda _key, _deadline: {},
            wait_timeout=self.operation_wait_timeout,
        )
        return _normalize_judgment(response)

    def _submit_judgment(
        self, store: PendingOperationStore, upload: _JudgeUpload
    ) -> Mapping[str, Any]:
        fields = {"metadata": json.dumps(upload.metadata, separators=(",", ":"))}
        parts = list(upload.log_parts)
        if upload.case_id is not None:
            fields["case_id"] = upload.case_id
        elif upload.case_part is not None:
            parts.insert(0, upload.case_part)

        def start_operation(key: str, deadline: float) -> Mapping[str, Any]:
            kwargs: dict[str, Any] = {"idempotency_key": key}
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
        )
        return _normalize_judgment(response)

    def judge_batch(
        self, contexts: Sequence[JudgeContext]
    ) -> tuple[JudgeBatchResult, ...]:
        """Judge completed Runs synchronously, preserving order and item errors."""

        _validate_batch_contexts(contexts)
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
        if not context.history:
            raise ProviderError("Judge requires at least one submitted Input")
        run_id = context.history[0].submission.run_id
        if any(item.submission.run_id != run_id for item in context.history):
            raise ProviderError("Judge history contains multiple Run IDs")
        return run_id

    @staticmethod
    def _submission_status(context: JudgeContext) -> str:
        for item in reversed(context.history):
            if item.submission.status != "completed":
                return item.submission.status
        return context.run_status


__all__ = ["OfficialJudgeProvider"]
