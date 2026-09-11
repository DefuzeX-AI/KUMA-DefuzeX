"""Project stored hash-only Runtime Evidence to named transport capabilities."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from ..contracts import FileChange, FileEvidence
from ..errors import LimitExceededError
from ..repository.privacy import scan_sensitive_text
from .runtime import project_runtime_evidence_v2
from .runtime_contract import (
    RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA,
    RUNTIME_EVIDENCE_CAPABILITY_ORDER,
    RUNTIME_EVIDENCE_MAX_BYTES,
    RUNTIME_FILE_DIFF_MAX_BYTES,
    RUNTIME_FILE_DIFF_TOTAL_MAX_BYTES,
    runtime_evidence_json,
    validate_runtime_evidence,
)


def _diff_omission_reason(change: FileChange) -> str:
    """Map local capture metadata to one frozen content-free wire reason.

    Args:
        change: File observation whose hash metadata remains uploadable.

    Returns:
        One canonical omission reason. Capture failures take precedence over
        text classification; a complete metadata-only change is reported as
        ``no_text_change`` rather than pretending a patch exists.

    Security/Privacy:
        Local reason text is inspected only for stable SDK codes and is never
        copied to the official wire.
    """

    reasons = set((change.reason or "").split(","))
    if not change.complete or reasons & {
        "changed_during_scan",
        "hash_size_limit",
        "mount_boundary",
        "read_failed",
        "readlink_failed",
        "special_file",
        "type_changed",
    }:
        return "capture_incomplete"
    if "size_limit" in reasons or "text_size_limit" in reasons:
        return "size_limit"
    if "sensitive_content" in reasons:
        return "sensitive_content"
    if "binary" in reasons:
        return "binary"
    return "no_text_change"


def _canonical_diff_text(change: FileChange, path: str) -> str | None:
    """Rewrite SDK-local diff headers to the bounded repository-relative wire path.

    Args:
        change: File change containing an optional complete local unified diff.
        path: Validated relative path from the matching v1 file component.

    Returns:
        A detached unified diff with canonical old/new headers, or ``None`` when
        no text was captured.

    Raises:
        ValueError: If captured text lacks the two headers required by the
            unified-diff contract.

    Side Effects:
        None; the local :class:`FileChange` remains unchanged.
    """

    if change.diff is None:
        return None
    lines = change.diff.splitlines(keepends=True)
    if (
        len(lines) < 3
        or not lines[0].startswith("--- ")
        or not lines[1].startswith("+++ ")
    ):
        raise ValueError("captured file diff is invalid")
    before = "/dev/null" if change.change_type == "created" else path
    after = "/dev/null" if change.change_type == "deleted" else path
    return "".join((f"--- {before}\n", f"+++ {after}\n", *lines[2:]))


def _wire_path_matches(local_path: str, wire_path: str) -> bool:
    """Match one SDK-owned absolute/relative path to its public relative path."""

    normalized = local_path.replace("\\", "/")
    return normalized == wire_path or normalized.endswith(f"/{wire_path}")


def _ordered_diff_sources(
    file_evidence: FileEvidence | None,
    components: list[Mapping[str, Any]],
) -> list[FileChange]:
    """Correlate retained v1 file components with SDK-owned local changes.

    Args:
        file_evidence: Submission file observations, or ``None``.
        components: Already validated public v1 file components after unsafe
            paths were dropped by the existing Runtime Evidence builder.

    Returns:
        File changes in component order; one rename can back its distinct
        delete/create facts.

    Raises:
        ValueError: If any retained public path has no unique local source.

    Security/Privacy:
        Matching uses only repository-relative suffixes and change types. Local
        absolute prefixes never enter the returned wire projection.
    """

    if file_evidence is None:
        if components:
            raise ValueError("runtime evidence file association is invalid")
        return []
    ordered = sorted(
        file_evidence.changes,
        key=lambda item: (item.path.casefold(), item.change_type, item.old_path or ""),
    )
    candidates: list[tuple[FileChange, str, str]] = []
    for change in ordered:
        if change.change_type == "renamed":
            candidates.extend(
                (
                    (change, change.old_path or "", "deleted"),
                    (change, change.path, "created"),
                )
            )
        else:
            candidates.append((change, change.path, change.change_type))
    result: list[FileChange] = []
    for component in components:
        matches = [
            index
            for index, (_, path, operation) in enumerate(candidates)
            if operation == component["change_type"]
            and _wire_path_matches(path, component["path"])
        ]
        if len(matches) != 1:
            raise ValueError("runtime evidence file association is invalid")
        change, _, _ = candidates.pop(matches[0])
        result.append(change)
    return result


def _included_diff(
    change: FileChange, component: Mapping[str, Any]
) -> dict[str, Any] | str:
    """Return one safe diff payload or a frozen omission reason.

    Args:
        change: Local source observation correlated by deterministic order.
        component: Detached validated v1 file component.

    Returns:
        A closed unified-diff object, or one of the canonical omission strings.

    Security/Privacy:
        Any sensitive marker causes content-free ``sensitive_content`` even
        when the Run allowed sensitive local artifacts. Raw findings never
        enter the returned reason.
    """

    if change.change_type == "renamed":
        return "no_text_change"
    try:
        text = _canonical_diff_text(change, component["path"])
    except ValueError:
        return "capture_incomplete"
    if text is None:
        return _diff_omission_reason(change)
    if scan_sensitive_text(text, location="file_diff"):
        return "sensitive_content"
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return "binary"
    if len(encoded) > RUNTIME_FILE_DIFF_MAX_BYTES:
        return "size_limit"
    return {
        "format": "unified",
        "text": text,
        "text_sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
    }


def _project_file_diffs(
    projected: dict[str, Any], file_evidence: FileEvidence | None
) -> None:
    """Attach deterministic diff outcomes within per-item and envelope budgets.

    Args:
        projected: Detached capability envelope being built for upload.
        file_evidence: Original immutable Submission file Evidence.

    Raises:
        ValueError: If local file observations no longer correlate one-to-one
            with the stored v1 file components.

    Postconditions:
        Every file component has exactly one ``diff`` or
        ``diff_omission_reason``; included text totals at most 65,536 bytes.
    """

    components = [
        component
        for component in projected["components"]
        if component["kind"] == "file_change"
    ]
    sources = _ordered_diff_sources(file_evidence, components)
    retained_bytes = 0
    for component, change in zip(components, sources, strict=True):
        outcome = _included_diff(change, component)
        if isinstance(outcome, str):
            component["diff_omission_reason"] = outcome
            continue
        if retained_bytes + outcome["utf8_bytes"] > RUNTIME_FILE_DIFF_TOTAL_MAX_BYTES:
            component["diff_omission_reason"] = "size_limit"
            continue
        component["diff"] = outcome
        retained_bytes += outcome["utf8_bytes"]


def _fit_capability_envelope(projected: dict[str, Any]) -> None:
    """Drop whole optional diffs until the complete envelope fits 5 MiB.

    Args:
        projected: Detached capability envelope after output and diff projection.

    Raises:
        LimitExceededError: If the hash-only metadata plus Agent output still
            exceeds the canonical item limit.

    Postconditions:
        No diff is truncated. Any removed patch becomes ``size_limit`` and the
        complete serialized envelope is within the frozen UTF-8 byte ceiling.
    """

    while (
        len(runtime_evidence_json(projected).encode("utf-8"))
        > RUNTIME_EVIDENCE_MAX_BYTES
    ):
        included = next(
            (
                component
                for component in reversed(projected["components"])
                if component["kind"] == "file_change" and "diff" in component
            ),
            None,
        )
        if included is None:
            raise LimitExceededError(
                "Runtime Evidence exceeds the canonical envelope limit",
                code="runtime_evidence_too_large",
                details={"max_utf8_bytes": RUNTIME_EVIDENCE_MAX_BYTES},
            )
        included.pop("diff")
        included["diff_omission_reason"] = "size_limit"


def project_runtime_evidence_capabilities(
    value: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
    status: str,
    output: Any,
    file_evidence: FileEvidence | None,
    upload_diff: bool,
    trace_evidence: Mapping[str, Any] | None = None,
    capture_status: str | None = None,
    capture_summary: Mapping[str, Any] | None = None,
    case_id: str | None = None,
) -> dict[str, Any]:
    """Build the negotiated named-capability transport view from stored v1.

    Args:
        value: Immutable local v1 envelope committed with the Submission.
        run_id: Expected owning Run identifier.
        input_id: Expected owning Submission input identifier.
        step_id: Expected public Case step identifier.
        submission_id: Deterministic Submission identity sent to Core.
        status: Submission status; only ``completed`` can expose output.
        output: Frozen final Agent output, hash-bound to the v1 claim.
        file_evidence: Correlated local file observations used only when
            ``upload_diff`` is explicit.
        upload_diff: Whether the user explicitly requested negotiated patches.
        trace_evidence: Already captured, privacy-filtered OTel envelope; when
            present it must be uploaded as body, never silently hash-only.
        capture_status: Actual trace component completeness, not tool status.
        capture_summary: Matching observation/drop counters from capture.
        case_id: Owning Judge Case used to verify nested Trace association.

    Returns:
        A detached closed ``defuzex.runtime_evidence.capabilities.v1`` mapping.

    Raises:
        LimitExceededError: If output or complete envelope exceeds its bound.
        ValueError: If association, claims, file correlation, or final schema is
            invalid. No partial or truncated patch is returned.

    Security/Privacy:
        The projection includes only final Agent output and explicitly requested
        safe diffs. Sensitive diffs become a content-free omission reason.
    """

    identifiers = {
        "run_id": run_id,
        "input_id": input_id,
        "step_id": step_id,
        "submission_id": submission_id,
    }
    projected = project_runtime_evidence_v2(
        value,
        **identifiers,
        status=status,
        output=output,
    )
    projected["schema_version"] = RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA
    projected["capabilities"] = list(RUNTIME_EVIDENCE_CAPABILITY_ORDER[:2])
    if upload_diff:
        projected["capabilities"].append("file_diff")
        _project_file_diffs(projected, file_evidence)
    if trace_evidence is not None:
        _project_trace(
            projected, trace_evidence, capture_status, capture_summary, case_id
        )
        projected["capabilities"].append("runtime_trace")
    _fit_capability_envelope(projected)
    validate_runtime_evidence(
        projected,
        **identifiers,
        schema_version=RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA,
    )
    return projected


def _project_trace(
    projected: dict[str, Any],
    trace: Mapping[str, Any],
    status: str | None,
    summary: Mapping[str, Any] | None,
    case_id: str | None,
) -> None:
    """Bind captured telemetry to its existing hash artifact before serialization.

    Called only after named Trace negotiation. It copies the immutable capture,
    checks the stored hash rather than repairing tampering, and requires matching
    Case/Run/Input identities and completeness. Invalid or missing correlation
    raises a value-free ValueError; sensitive data raises before multipart.
    There is no body reconstruction, truncation, new execution or I/O.
    """
    from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_json
    from .runtime_trace_contract import TRACE_ARTIFACT_ID, validate_trace_artifact

    enforce_sensitive_policy(
        scan_sensitive_json(trace, location="runtime_trace"), allow_sensitive=False
    )
    matches = [
        c
        for c in projected["components"]
        if c.get("kind") == "artifact_snapshot"
        and c.get("artifact_id") == TRACE_ARTIFACT_ID
    ]
    if len(matches) != 1:
        raise ValueError("Runtime Trace artifact is unavailable")
    component = matches[0]
    # runtime_evidence_json detaches through the canonical JSON boundary.
    import json

    component.update(
        trace_evidence=json.loads(runtime_evidence_json(trace)),
        capture_status=status,
        capture_summary=None
        if summary is None
        else json.loads(runtime_evidence_json(summary)),
    )
    validate_trace_artifact(
        component,
        run_id=projected["run_id"],
        input_id=projected["input_id"],
        case_id=case_id,
    )


__all__ = ["project_runtime_evidence_capabilities"]
