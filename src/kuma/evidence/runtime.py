"""Build and validate canonical, hash-only Runtime Evidence items."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import FileChange, FileEvidence
from ..repository.privacy import scan_sensitive_path
from .runtime_contract import (
    RUNTIME_EVIDENCE_MAX_CHARS,
    RUNTIME_EVIDENCE_SCHEMA,
    normalize_sha256,
    runtime_evidence_json,
    validate_runtime_evidence,
)


@dataclass(frozen=True, slots=True)
class RuntimeEvidenceLimits:
    """Internal resource limits bounded by the frozen Core contract."""

    max_components: int = 100
    max_text_length: int = 1024
    max_content_chars: int = RUNTIME_EVIDENCE_MAX_CHARS

    def __post_init__(self) -> None:
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
    evidence: Mapping[str, Any]
    missing: tuple[str, ...] = ()
    dropped_count: int = 0


def runtime_submission_id(run_id: str, input_id: str) -> str:
    """Return the stable public Submission identity used for retries/replays."""

    digest = hashlib.sha256(
        f"defuzex-runtime-submission-v1\0{run_id}\0{input_id}".encode()
    ).hexdigest()
    return f"submission-{digest[:32]}"


def _sha256(value: Any) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8", "surrogatepass")
    else:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _relative_path(value: Any, root: Path, limit: int) -> str | None:
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
        component: dict[str, Any] = {
            "kind": "artifact_snapshot",
            "artifact_id": f"log-segment-{index}",
            "sha256": digest,
            "size_bytes": end - start,
            "media_type": "text/plain",
        }
        path = _relative_path(segment.get("path"), root, limits.max_text_length)
        if path is not None:
            component["path"] = path
        result.append(component)
    return result, dropped


def _trace_component(trace_evidence: Mapping[str, Any] | None) -> dict[str, Any] | None:
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
    claim_text = output if output is not None else error or ""
    return {
        "kind": "agent_response_claim",
        "claim_id": "submission-response",
        "claim": "completed" if status == "completed" else "blocked",
        "text_sha256": _sha256(claim_text),
    }


def _with_component_ids(
    components: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
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
    "runtime_submission_id",
]
