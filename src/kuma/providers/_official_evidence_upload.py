"""Validate and serialize legacy or canonical Official Judge Evidence uploads."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import LimitExceededError, ProviderError
from ..evidence.runtime import project_runtime_evidence_v2, runtime_submission_id
from ..evidence.runtime_capabilities import project_runtime_evidence_capabilities
from ..evidence.runtime_contract import (
    RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA,
    RUNTIME_EVIDENCE_CAPABILITY_ORDER,
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
        runtime_evidence_capabilities: Validated ordered named capabilities.
            Missing advertisement means Trace is unsupported, not hash fallback.
    """

    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    manifest_schema_version: str
    max_batch_items: int
    evidence_types: frozenset[str]
    runtime_evidence_capabilities: tuple[str, ...] = ()


def judge_upload_config(response: Mapping[str, Any]) -> JudgeUploadConfig:
    """Validate the public Backend's dynamic Judge upload contract."""

    max_files = response.get("max_files")
    max_file_bytes = response.get("max_file_bytes")
    max_total_bytes = response.get("max_total_bytes")
    allowed = response.get("allowed_extensions")
    manifest_schema_version = response.get("manifest_schema_version")
    evidence_types = response.get("evidence_types")
    max_batch_items = response.get("max_batch_items", _MAX_BATCH_ITEMS)
    capabilities = response.get("runtime_evidence_capabilities")
    if "runtime_evidence_capabilities" in response and (
        not isinstance(capabilities, list)
        or capabilities
        not in [
            list(RUNTIME_EVIDENCE_CAPABILITY_ORDER[:2]),
            list(RUNTIME_EVIDENCE_CAPABILITY_ORDER[:3]),
            [*RUNTIME_EVIDENCE_CAPABILITY_ORDER[:2], "runtime_trace"],
            list(RUNTIME_EVIDENCE_CAPABILITY_ORDER),
        ]
        or not isinstance(evidence_types, list)
        or RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA not in evidence_types
    ):
        raise ProviderError(
            "The Backend returned invalid Trace capability configuration",
            code="invalid_response",
        )
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
        runtime_evidence_capabilities=tuple(capabilities or ()),
    )


def _runtime_evidence_part(
    item: Any,
    *,
    index: int,
    max_file_bytes: int,
    part_prefix: str,
    schema_version: str = RUNTIME_EVIDENCE_SCHEMA_V1,
    upload_diff: bool = False,
) -> tuple[UploadPart, dict[str, Any], list[Any]] | None:
    """Build one negotiated Runtime Evidence part from stored v1 history.

    The local Submission extension remains v1. When the Backend explicitly
    advertises v2 or the named-capability schema, this boundary creates a
    detached output-bearing view and performs a mandatory Agent-output scan that
    ignores ``allow_sensitive``. Explicit ``upload_diff`` additionally projects
    only bounded safe unified diffs. No multipart object is returned until
    association, privacy, schema, and byte limits pass.

    Args:
        item: One immutable Run history item and its stored v1 envelope.
        index: Stable zero-based history position used only in safe filenames.
        max_file_bytes: Backend-advertised limit for the complete encoded part;
            the SDK's 5 MiB Runtime Evidence ceiling also applies.
        part_prefix: Optional batch prefix for multipart field names.
        schema_version: Exact public schema selected from Backend config.
        upload_diff: Whether this Run explicitly requested ``file_diff``.

    Returns:
        Upload part, manifest entry, and findings; ``None`` only when the stored
        Submission has no Runtime Evidence extension.

    Raises:
        ProviderError: If stored/projected Evidence is malformed or misbound.
        LimitExceededError: If a canonical output or complete part is too large.
        SensitiveDataError: If completed Agent output contains sensitive data.

    Security/Privacy:
        This is the final SDK boundary before multipart construction. It never
        uses ``allow_sensitive`` to expose Agent output or diff text.
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
        if schema_version in {
            RUNTIME_EVIDENCE_SCHEMA_V2,
            RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA,
        }:
            if item.submission.status == "completed":
                output_findings = scan_sensitive_json(
                    item.submission.output, location="agent_output"
                )
                enforce_sensitive_policy(output_findings, allow_sensitive=False)
            projection_args = {
                "run_id": item.submission.run_id,
                "input_id": item.submission.input_id,
                "step_id": item.test_input.input_id,
                "submission_id": submission_id,
                "status": item.submission.status,
                "output": item.submission.output,
            }
            if schema_version == RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA:
                value = project_runtime_evidence_capabilities(
                    value,
                    file_evidence=item.submission.file_evidence,
                    upload_diff=upload_diff,
                    trace_evidence=item.submission.extensions.get("trace_evidence"),
                    capture_summary=item.submission.extensions.get(
                        "trace_capture_summary"
                    ),
                    capture_status=item.submission.capture_status.traces.status,
                    case_id=item.submission.case_id,
                    **projection_args,
                )
            else:
                value = project_runtime_evidence_v2(value, **projection_args)
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
    """Collect the negotiated semantic schema or a historical compatible wire.

    Captured Trace requires explicit ``runtime_trace`` advertisement. Explicit
    file-diff upload requires the named ``file_diff`` capability and
    never falls back to v1/v2 or legacy raw logs. Default hash-only Runs prefer
    the named schema when advertised, then retain historical v2/v1 support.

    Args:
        context: Completed Run history plus its explicit diff-upload choice.
        config: Validated public Backend upload limits and supported schemas.
        part_prefix: Optional batch-safe multipart field prefix.

    Returns:
        Ordered parts, matching manifest entries, and privacy findings.

    Raises:
        ProviderError: If explicit diff upload is unsupported or no typed
            Runtime Evidence exists for that explicit request.

    Postconditions:
        An explicit diff request either returns named-capability parts for every
        available step or fails before any Judge POST; it never downgrades.
    """
    trace_enabled = any(
        "trace_evidence" in item.submission.extensions for item in context.history
    )
    _validate_trace_associations(context)
    if (
        context.upload_diff
        and config.runtime_evidence_capabilities
        and "file_diff" not in config.runtime_evidence_capabilities
    ):
        raise ProviderError(
            "The Backend does not support file-diff Evidence",
            code="runtime_evidence_unsupported",
        )
    if trace_enabled and "runtime_trace" not in config.runtime_evidence_capabilities:
        raise ProviderError(
            "The Backend does not support captured Runtime Trace Evidence",
            code="runtime_evidence_unsupported",
        )
    if context.upload_diff or trace_enabled:
        if RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA not in config.evidence_types:
            raise ProviderError(
                "The Backend does not support file-diff Evidence",
                code="runtime_evidence_unsupported",
            )
        schema_version = RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA
    elif RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA in config.evidence_types:
        schema_version = RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA
    elif RUNTIME_EVIDENCE_SCHEMA_V2 in config.evidence_types:
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
            upload_diff=context.upload_diff,
        )
        if built is None:
            continue
        part, entry, item_findings = built
        parts.append(part)
        manifest.append(entry)
        findings.extend(item_findings)
    if (context.upload_diff or trace_enabled) and not parts:
        raise ProviderError(
            "File-diff Evidence is unavailable for this Run",
            code="runtime_evidence_invalid",
        )
    return parts, manifest, findings


def _validate_trace_associations(context: JudgeContext) -> None:
    """Reject invalid Trace association before compatibility checks.

    Preserve safe error precedence without invoking legacy upload or changing
    history; detailed named-schema validation follows negotiation.
    """
    for item in context.history:
        if "trace_evidence" not in item.submission.extensions:
            continue
        trace = item.submission.extensions["trace_evidence"]
        if (
            not isinstance(trace, Mapping)
            or set(trace)
            != {
                "schema_version",
                "run_id",
                "case_id",
                "input_id",
                "spans",
                "dropped_count",
                "truncated",
                "reasons",
            }
            or (trace.get("run_id"), trace.get("case_id"), trace.get("input_id"))
            != (
                item.submission.run_id,
                item.submission.case_id,
                item.submission.input_id,
            )
            or item.submission.case_id != context.case.case_id
        ):
            raise ProviderError(
                "Trace Evidence is invalid", code="trace_evidence_invalid"
            )


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
