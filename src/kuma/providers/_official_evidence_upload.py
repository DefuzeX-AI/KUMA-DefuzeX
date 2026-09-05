"""Validate and serialize legacy or canonical Official Judge Evidence uploads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import LimitExceededError, ProviderError
from ..evidence.runtime import project_runtime_evidence_v2, runtime_submission_id
from ..evidence.runtime_contract import (
    RUNTIME_EVIDENCE_MAX_BYTES,
    RUNTIME_EVIDENCE_MEDIA_TYPE,
    RUNTIME_EVIDENCE_SCHEMA_V1,
    RUNTIME_EVIDENCE_SCHEMA_V2,
    runtime_evidence_json,
    validate_runtime_evidence,
)
from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_json
from ..transport.backend import UploadPart
from ._evidence_projection import project_run_evidence
from ._official_wire import history_evidence, plain_json
from .base import JudgeContext

_MAX_BATCH_ITEMS = 20


@dataclass(frozen=True, slots=True)
class JudgeUploadConfig:
    """Hold the validated dynamic public Judge upload limits.

    Attributes:
        max_files: Maximum multipart Evidence files accepted per request.
        max_file_bytes: Maximum bytes accepted for one Evidence part.
        max_total_bytes: Maximum combined multipart Evidence bytes.
        manifest_schema_version: Exact public manifest version to serialize.
        max_batch_items: Maximum Runs accepted by synchronous batch Judge.
        evidence_types: Closed public Evidence media/type identifiers advertised
            by the Backend.
    """

    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    manifest_schema_version: str
    max_batch_items: int
    evidence_types: frozenset[str]


def judge_upload_config(response: Mapping[str, Any]) -> JudgeUploadConfig:
    """Validate the public Backend's dynamic Judge upload contract."""

    max_files = response.get("max_files")
    max_file_bytes = response.get("max_file_bytes")
    max_total_bytes = response.get("max_total_bytes")
    allowed = response.get("allowed_extensions")
    manifest_schema_version = response.get("manifest_schema_version")
    evidence_types = response.get("evidence_types")
    max_batch_items = response.get("max_batch_items", _MAX_BATCH_ITEMS)
    if (
        isinstance(max_files, bool)
        or not isinstance(max_files, int)
        or max_files < 1
        or isinstance(max_file_bytes, bool)
        or not isinstance(max_file_bytes, int)
        or max_file_bytes <= 0
        or isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes <= 0
        or not isinstance(allowed, list)
        or ".json" not in allowed
        or not isinstance(manifest_schema_version, str)
        or not manifest_schema_version
        or not isinstance(evidence_types, list)
        or "raw_log" not in evidence_types
        or isinstance(max_batch_items, bool)
        or not isinstance(max_batch_items, int)
        or max_batch_items < 1
    ):
        raise ProviderError(
            "The Backend returned invalid Judge upload configuration",
            code="invalid_response",
        )
    return JudgeUploadConfig(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        manifest_schema_version=manifest_schema_version,
        max_batch_items=max_batch_items,
        evidence_types=frozenset(evidence_types),
    )


def _runtime_evidence_part(
    item: Any,
    *,
    index: int,
    max_file_bytes: int,
    part_prefix: str,
    schema_version: str = RUNTIME_EVIDENCE_SCHEMA_V1,
) -> tuple[UploadPart, dict[str, Any], list[Any]] | None:
    """Build one negotiated Runtime Evidence part from stored v1 history.

    The local Submission extension remains v1. When the Backend explicitly
    advertises v2, this boundary creates a detached v2 view and performs a
    mandatory Agent-output sensitive scan that ignores ``allow_sensitive``.
    No multipart object is returned until association, privacy, schema, and byte
    limits pass.
    """
    value = item.submission.extensions.get("runtime_evidence")
    if value is None:
        return None
    submission_id = runtime_submission_id(
        item.submission.run_id, item.submission.input_id
    )
    try:
        validate_runtime_evidence(
            value,
            run_id=item.submission.run_id,
            input_id=item.submission.input_id,
            step_id=item.test_input.input_id,
            submission_id=submission_id,
            schema_version=RUNTIME_EVIDENCE_SCHEMA_V1,
        )
        if schema_version == RUNTIME_EVIDENCE_SCHEMA_V2:
            if item.submission.status == "completed":
                output_findings = scan_sensitive_json(
                    item.submission.output, location="agent_output"
                )
                enforce_sensitive_policy(output_findings, allow_sensitive=False)
            value = project_runtime_evidence_v2(
                value,
                run_id=item.submission.run_id,
                input_id=item.submission.input_id,
                step_id=item.test_input.input_id,
                submission_id=submission_id,
                status=item.submission.status,
                output=item.submission.output,
            )
    except ValueError as exc:
        raise ProviderError(
            "Runtime Evidence is invalid", code="runtime_evidence_invalid"
        ) from exc
    encoded = runtime_evidence_json(value).encode()
    if len(encoded) > min(max_file_bytes, RUNTIME_EVIDENCE_MAX_BYTES):
        raise LimitExceededError(
            "Runtime Evidence exceeds the Judge upload limit",
            code="log_size_exceeded",
        )
    filename = f"kuma-runtime-evidence-{index:04d}.json"
    return (
        UploadPart(
            name=f"{part_prefix}runtime-{index}" if part_prefix else "logs",
            filename=filename,
            content_type=RUNTIME_EVIDENCE_MEDIA_TYPE,
            data=encoded,
        ),
        {
            "name": filename,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "evidence_type": schema_version,
        },
        scan_sensitive_json(value, location="runtime_evidence"),
    )


def _runtime_evidence_parts(
    context: JudgeContext, config: JudgeUploadConfig, part_prefix: str
) -> tuple[list[UploadPart], list[dict[str, Any]], list[Any]]:
    """Collect the highest explicitly negotiated Runtime Evidence version."""
    if RUNTIME_EVIDENCE_SCHEMA_V2 in config.evidence_types:
        schema_version = RUNTIME_EVIDENCE_SCHEMA_V2
    elif RUNTIME_EVIDENCE_SCHEMA_V1 in config.evidence_types:
        schema_version = RUNTIME_EVIDENCE_SCHEMA_V1
    else:
        return [], [], []
    parts: list[UploadPart] = []
    manifest: list[dict[str, Any]] = []
    findings: list[Any] = []
    for index, item in enumerate(context.history):
        built = _runtime_evidence_part(
            item,
            index=index,
            max_file_bytes=config.max_file_bytes,
            part_prefix=part_prefix,
            schema_version=schema_version,
        )
        if built is None:
            continue
        part, entry, item_findings = built
        parts.append(part)
        manifest.append(entry)
        findings.extend(item_findings)
    return parts, manifest, findings


def _typed_upload(
    parts: list[UploadPart],
    manifest: list[dict[str, Any]],
    findings: list[Any],
    config: JudgeUploadConfig,
) -> tuple[tuple[UploadPart, ...], dict[str, Any], list[Any]]:
    """Enforce dynamic file and byte limits for canonical typed Evidence."""
    if len(parts) > config.max_files:
        raise LimitExceededError(
            "Runtime Evidence exceeds the Judge file-count limit",
            code="log_size_exceeded",
        )
    if sum(len(part.data) for part in parts) > config.max_total_bytes:
        raise LimitExceededError(
            "Runtime Evidence exceeds the Judge upload limit",
            code="log_size_exceeded",
        )
    return (
        tuple(parts),
        {"schema_version": config.manifest_schema_version, "files": manifest},
        findings,
    )


def _legacy_upload(
    context: JudgeContext, config: JudgeUploadConfig, part_prefix: str
) -> tuple[tuple[UploadPart, ...], dict[str, Any], list[Any]]:
    """Project complete history into one bounded legacy Evidence file."""
    evidence = {
        "schema_version": "defuzex.run_evidence.v1",
        "run_status": context.run_status,
        "history": history_evidence(context),
        "summary": plain_json(context.evidence_summary),
    }
    findings = list(scan_sensitive_json(evidence, location="judge_evidence"))
    evidence, evidence_bytes = project_run_evidence(
        evidence,
        max_utf8_bytes=min(config.max_file_bytes, config.max_total_bytes),
    )
    filename = "kuma-run-evidence.json"
    part = UploadPart(
        name=f"{part_prefix}log" if part_prefix else "logs",
        filename=filename,
        content_type="application/json",
        data=evidence_bytes,
    )
    manifest = {
        "schema_version": config.manifest_schema_version,
        "files": [
            {
                "name": filename,
                "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "evidence_type": "raw_log",
            }
        ],
    }
    return ((part,), manifest, findings)


def evidence_upload(
    context: JudgeContext, config: JudgeUploadConfig, part_prefix: str
) -> tuple[tuple[UploadPart, ...], dict[str, Any], list[Any]]:
    """Build typed evidence when negotiated, otherwise the legacy run item."""

    runtime_parts, runtime_manifest, findings = _runtime_evidence_parts(
        context, config, part_prefix
    )
    if runtime_parts:
        return _typed_upload(runtime_parts, runtime_manifest, findings, config)
    return _legacy_upload(context, config, part_prefix)


__all__ = ["JudgeUploadConfig", "evidence_upload", "judge_upload_config"]
