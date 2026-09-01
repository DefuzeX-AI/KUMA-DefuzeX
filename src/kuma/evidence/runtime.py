"""Build local hash-only v1 Evidence and negotiated v2 transport views."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._json_values import JsonStructureError, detach_json
from ..contracts import FileChange, FileEvidence
from ..errors import LimitExceededError
from ..repository.privacy import scan_sensitive_path
from .otel_log_mapping import OTEL_LOG_MEDIA_TYPE
from .runtime_contract import (
    RUNTIME_AGENT_OUTPUT_MAX_BYTES,
    RUNTIME_EVIDENCE_MAX_CHARS,
    RUNTIME_EVIDENCE_SCHEMA,
    RUNTIME_EVIDENCE_SCHEMA_V1,
    RUNTIME_EVIDENCE_SCHEMA_V2,
    normalize_sha256,
    runtime_agent_output_bytes,
    runtime_claim_sha256,
    runtime_evidence_json,
    validate_runtime_evidence,
)


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceLimits:
    """Bound one canonical Runtime Evidence envelope before official upload.

    Attributes:
        max_components: Maximum typed facts retained in one step envelope; the
            Core v1 contract permits at most 100.
        max_text_length: Maximum characters accepted for a safe relative path or
            other bounded identifier before that observation is dropped.
        max_content_chars: Maximum characters in the complete canonical JSON
            content, including identifiers and component envelope overhead.
    """

    max_components: int = 100
    max_text_length: int = 1024
    max_content_chars: int = RUNTIME_EVIDENCE_MAX_CHARS

    def __post_init__(self) -> None:
        """Enforce Core-compatible component, text, and envelope size limits."""
        values = (self.max_components, self.max_text_length, self.max_content_chars)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("runtime evidence limits must be integers")
        if not 1 <= self.max_components <= 100 or self.max_text_length <= 0:
            raise ValueError("runtime evidence item limits are invalid")
        if not 1024 <= self.max_content_chars <= RUNTIME_EVIDENCE_MAX_CHARS:
            raise ValueError("runtime evidence character limit is invalid")


@dataclass(frozen=True, slots=True)
class BuiltRuntimeEvidence:
    """Return a canonical envelope together with transparent loss accounting.

    Attributes:
        evidence: Closed ``defuzex.runtime_evidence.v1`` mapping ready for
            serialization as one typed Evidence item.
        missing: Stable reasons explaining unavailable or filtered observations.
        dropped_count: Total observations/components omitted for privacy,
            validity, component-count, or character-budget reasons.
    """

    evidence: Mapping[str, Any]
    missing: tuple[str, ...] = ()
    dropped_count: int = 0


def runtime_submission_id(run_id: str, input_id: str) -> str:
    """Return the stable public Submission identity used for retries/replays."""

    digest = hashlib.sha256(
        f"defuzex-runtime-submission-v1\0{run_id}\0{input_id}".encode()
    ).hexdigest()
    return f"submission-{digest[:32]}"


def _relative_path(value: Any, root: Path, limit: int) -> str | None:
    """Return a bounded non-sensitive path only when it remains under the Run root."""
    try:
        relative = Path(value).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, TypeError, ValueError):
        return None
    if (
        not relative
        or len(relative) > limit
        or scan_sensitive_path(relative, location="runtime_evidence_path")
    ):
        return None
    return relative


def _file_component(change: FileChange, *, path: str, operation: str) -> dict[str, Any]:
    """Project one file observation to canonical hash-only wire fields."""
    result: dict[str, Any] = {
        "kind": "file_change",
        "path": path,
        "change_type": operation,
    }
    size = change.before_size if operation == "deleted" else change.after_size
    if size is not None:
        result["size_bytes"] = size
    before = normalize_sha256(change.before_hash)
    after = normalize_sha256(change.after_hash)
    if before is not None:
        result["before_sha256"] = before
    if after is not None:
        result["after_sha256"] = after
    return result


def _file_components(
    evidence: FileEvidence | None,
    root: Path,
    limits: RuntimeEvidenceLimits,
) -> tuple[list[dict[str, Any]], int]:
    """Order file changes, expand renames, and count unsafe paths as dropped."""
    if evidence is None:
        return [], 0
    result: list[dict[str, Any]] = []
    dropped = 0
    ordered = sorted(
        evidence.changes,
        key=lambda item: (item.path.casefold(), item.change_type, item.old_path or ""),
    )
    for change in ordered:
        path = _relative_path(change.path, root, limits.max_text_length)
        if path is None:
            dropped += 1
            continue
        if change.change_type == "renamed":
            old_path = _relative_path(change.old_path, root, limits.max_text_length)
            if old_path is None:
                dropped += 1
                continue
            result.append(_file_component(change, path=old_path, operation="deleted"))
            result.append(_file_component(change, path=path, operation="created"))
        else:
            result.append(
                _file_component(change, path=path, operation=change.change_type)
            )
    return result, dropped


def _log_components(
    logs: Sequence[Mapping[str, Any]],
    root: Path,
    limits: RuntimeEvidenceLimits,
) -> tuple[list[dict[str, Any]], int]:
    """Represent captured log segments as hashed artifact snapshots."""
    result: list[dict[str, Any]] = []
    dropped = 0
    for index, segment in enumerate(logs):
        digest = normalize_sha256(segment.get("sha256"))
        start, end = segment.get("start_offset"), segment.get("end_offset")
        if (
            digest is None
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not 0 <= start <= end
        ):
            dropped += 1
            continue
        media_type = segment.get("media_type")
        component: dict[str, Any] = {
            "kind": "artifact_snapshot",
            "artifact_id": f"log-segment-{index}",
            "sha256": digest,
            "size_bytes": end - start,
            "media_type": (
                OTEL_LOG_MEDIA_TYPE
                if media_type == OTEL_LOG_MEDIA_TYPE
                else "text/plain"
            ),
        }
        path = _relative_path(segment.get("path"), root, limits.max_text_length)
        if path is not None:
            component["path"] = path
        result.append(component)
    return result, dropped


def _trace_component(trace_evidence: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Represent canonical Trace Evidence as a hash-only artifact snapshot."""
    if trace_evidence is None:
        return None
    text = runtime_evidence_json(trace_evidence)
    return {
        "kind": "artifact_snapshot",
        "artifact_id": "opentelemetry-trace-evidence",
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size_bytes": len(text.encode("utf-8")),
        "media_type": "application/vnd.defuzex.trace-evidence+json",
    }


def _claim_component(status: str, output: Any, error: str | None) -> dict[str, Any]:
    """Hash the Agent response and label it explicitly as an unverified claim."""
    claim_text = output if output is not None else error or ""
    return {
        "kind": "agent_response_claim",
        "claim_id": "submission-response",
        "claim": "completed" if status == "completed" else "blocked",
        "text_sha256": runtime_claim_sha256(claim_text),
    }


def _bounded_plain_agent_output(output: Any) -> tuple[Any, str]:
    """Detach one v2 output and return it with its frozen claim digest.

    Structural failures become value-free ``ValueError`` instances. The
    32,768-byte limit is measured against canonical JSON and raises a public
    ``LimitExceededError`` instead of truncating the Agent result.
    """

    try:
        plain_output = detach_json(output)
        output_bytes = runtime_agent_output_bytes(plain_output)
        digest = runtime_claim_sha256(plain_output)
    except (JsonStructureError, RecursionError, TypeError, ValueError):
        raise ValueError("runtime evidence Agent output is invalid") from None
    if len(output_bytes) > RUNTIME_AGENT_OUTPUT_MAX_BYTES:
        raise LimitExceededError(
            "Agent output exceeds the Runtime Evidence upload limit",
            code="agent_output_too_large",
            details={"max_utf8_bytes": RUNTIME_AGENT_OUTPUT_MAX_BYTES},
        )
    return plain_output, digest


def _project_v2_claim(
    projected: Mapping[str, Any], *, status: str, output: Any
) -> None:
    """Mutate only the detached v2 claim after status/hash/size validation."""

    claims = [
        component
        for component in projected["components"]
        if component["kind"] == "agent_response_claim"
    ]
    if len(claims) != 1:
        raise ValueError("runtime evidence must contain exactly one response claim")
    claim = claims[0]
    if status == "completed":
        if claim["claim"] != "completed" or output is None:
            raise ValueError("runtime evidence completed claim is invalid")
        plain_output, digest = _bounded_plain_agent_output(output)
        if digest != claim["text_sha256"]:
            raise ValueError("runtime evidence Agent output hash is invalid")
        claim["agent_output"] = plain_output
    elif claim["claim"] == "completed":
        raise ValueError("runtime evidence claim does not match Submission status")


def project_runtime_evidence_v2(
    value: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
    status: str,
    output: Any,
) -> dict[str, Any]:
    """Create the negotiated v2 transport view of one stored v1 envelope.

    Args:
        value: Immutable v1 envelope captured transactionally with the
            Submission. It remains unchanged in local history and persistence.
        run_id: Expected Run association from the owning Submission.
        input_id: Expected Input association from the owning Submission.
        step_id: Expected public Case step identifier.
        submission_id: Deterministic Submission identifier used by Core.
        status: Owning Submission status. Only ``completed`` exposes output.
        output: Frozen Submission output; it is detached into plain JSON before
            inclusion and must match the existing v1 claim digest.

    Returns:
        A new closed ``defuzex.runtime_evidence.v2`` mapping. The original v1
        extension and caller-owned values are never mutated.

    Raises:
        LimitExceededError: If canonical Agent output exceeds 32,768 UTF-8
            bytes or adding it would exceed the 120,000-character envelope cap.
        ValueError: If v1 association, claim cardinality/status/hash, output JSON,
            or the resulting v2 envelope is invalid.

    Security/Privacy:
        This pure projection adds only the final Agent output. Sensitive-data
        enforcement is deliberately performed by the official upload boundary
        immediately before multipart construction.
    """

    identifiers = {
        "run_id": run_id,
        "input_id": input_id,
        "step_id": step_id,
        "submission_id": submission_id,
    }
    validate_runtime_evidence(
        value,
        **identifiers,
        schema_version=RUNTIME_EVIDENCE_SCHEMA_V1,
    )
    projected = json.loads(runtime_evidence_json(value))
    _project_v2_claim(projected, status=status, output=output)
    projected["schema_version"] = RUNTIME_EVIDENCE_SCHEMA_V2
    if len(runtime_evidence_json(projected)) > RUNTIME_EVIDENCE_MAX_CHARS:
        raise LimitExceededError(
            "Runtime Evidence exceeds the canonical envelope limit",
            code="runtime_evidence_too_large",
            details={"max_characters": RUNTIME_EVIDENCE_MAX_CHARS},
        )
    validate_runtime_evidence(
        projected,
        **identifiers,
        schema_version=RUNTIME_EVIDENCE_SCHEMA_V2,
    )
    return projected


def _with_component_ids(
    components: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Assign deterministic unique component IDs and monotonic sequence numbers."""
    return [
        {
            "component_id": f"component-{sequence:04d}",
            "sequence": sequence,
            **component,
        }
        for sequence, component in enumerate(components)
    ]


def _envelope(
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
    components: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the closed association envelope consumed by Core Judge."""
    return {
        "schema_version": RUNTIME_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "input_id": input_id,
        "step_id": step_id,
        "submission_id": submission_id,
        "components": _with_component_ids(components),
    }


def _fit_components(
    components: list[dict[str, Any]],
    *,
    identifiers: Mapping[str, str],
    limits: RuntimeEvidenceLimits,
) -> tuple[dict[str, Any], int, bool]:
    """Retain ordered facts and the final claim within component and character caps."""
    dropped = max(0, len(components) - limits.max_components)
    retained = components[: limits.max_components]
    if dropped and limits.max_components > 1:
        retained[-1] = components[-1]
    elif dropped:
        retained = [components[-1]]
    envelope = _envelope(**identifiers, components=retained)
    char_limited = False
    while len(runtime_evidence_json(envelope)) > limits.max_content_chars:
        if len(retained) <= 1:
            raise ValueError("runtime evidence metadata exceeds the character limit")
        retained.pop(-2)
        dropped += 1
        char_limited = True
        envelope = _envelope(**identifiers, components=retained)
    return envelope, dropped, char_limited


def build_runtime_evidence(
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
    root: Path,
    status: str,
    output: Any,
    error: str | None,
    file_evidence: FileEvidence | None,
    logs: Sequence[Mapping[str, Any]],
    trace_evidence: Mapping[str, Any] | None,
    limits: RuntimeEvidenceLimits | None = None,
) -> BuiltRuntimeEvidence:
    """Build one closed canonical envelope without raw Agent or tool content."""

    active_limits = limits or RuntimeEvidenceLimits()
    files, file_dropped = _file_components(file_evidence, root, active_limits)
    artifacts, log_dropped = _log_components(logs, root, active_limits)
    trace = _trace_component(trace_evidence)
    components = [*files, *artifacts]
    if trace is not None:
        components.append(trace)
    components.append(_claim_component(status, output, error))
    identifiers = {
        "run_id": run_id,
        "input_id": input_id,
        "step_id": step_id,
        "submission_id": submission_id,
    }
    envelope, limit_dropped, char_limited = _fit_components(
        components, identifiers=identifiers, limits=active_limits
    )
    validate_runtime_evidence(envelope, **identifiers)
    missing = []
    if file_dropped or log_dropped:
        missing.append("runtime_evidence_observation_filtered")
    if limit_dropped:
        missing.append("runtime_evidence_component_limit")
    if char_limited:
        missing.append("runtime_evidence_character_limit")
    return BuiltRuntimeEvidence(
        evidence=envelope,
        missing=tuple(missing),
        dropped_count=file_dropped + log_dropped + limit_dropped,
    )


__all__ = [
    "BuiltRuntimeEvidence",
    "RuntimeEvidenceLimits",
    "build_runtime_evidence",
    "project_runtime_evidence_v2",
    "runtime_submission_id",
]
