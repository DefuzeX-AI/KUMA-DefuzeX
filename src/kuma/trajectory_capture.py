"""Connect existing KUMA observation capture to review preparation without duplicate collectors."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from . import observation_contract
from .errors import ValidationError
from .trajectory import prepare_review
from .trajectory_requirements import requirements_fingerprint, validate_requirements

_OPERATIONS = {
    "execute_tool": "tool",
    "chat": "model",
    "text_completion": "model",
    "generate_content": "model",
    "embeddings": "embedding",
    "retrieval": "retrieval",
    "invoke_agent": "agent",
    "create_agent": "agent",
}


def classify_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Group recorded operations structurally, without turning errors into Agent fault.

    Args:
        observation: Completed kuma.observe().export() data, validated using its existing contract.
    Returns:
        v1 inventory of exact trace/span/parent IDs, operation classes, recorded statuses and
        durations; unmapped operations are other. Counts describe retained spans only. Includes
        capture status/gaps; no semantic task category, retry blame, success or cost is inferred.
    Raises:
        ValidationError: Invalid, sensitive or oversized observation.
    Side Effects:
        None; no collector, Agent, tool, network or filesystem operation.
    """
    safe = observation_contract.checked(observation_contract.export, observation)
    spans = []
    counts: Counter[str] = Counter()
    for item in safe["spans"]:
        operation = item["attributes"].get("gen_ai.operation.name")
        category = (
            _OPERATIONS.get(operation, "other")
            if isinstance(operation, str)
            else "other"
        )
        counts[category] += 1
        spans.append(
            {
                "trace_id": item["trace_id"],
                "span_id": item["span_id"],
                "parent_span_id": item["parent_span_id"],
                "category": category,
                "operation": operation,
                "status": item["status"],
                "duration_nano": item["duration_nano"],
            }
        )
    return {
        "schema_version": "kuma.trajectory_inventory.v1",
        "counts": dict(counts),
        "spans": spans,
        "capture_status": safe["capture_status"],
        "reasons": safe["reasons"],
    }


def prepare_observation(
    observation: Mapping[str, Any],
    requirements: Mapping[str, Any],
    *,
    sample: str = "trajectory",
) -> dict[str, Any]:
    """Prepare actual observed spans with caller requirements and explicit capture gaps.

    Args:
        observation: Completed local export from existing kuma.observe; never an active session.
        requirements: Reviewed v1 task/tool/retry/budget document; full field guide in platform docs.
        sample: Review label, nonblank up to 200 characters, defaults to trajectory.
    Returns:
        kuma.trajectory_submission.v1 with detached bundle, requirements, content revision,
        recorded observation ID and structural inventory. Empty capture is represented by a
        genuine capture-summary event, never fabricated Agent activity. No turn IDs are inferred.
    Raises:
        ValidationError: Invalid input, unsafe policies or complete review bundle above 4 MiB.
    Side Effects:
        None; importing/preparing does not upload, evaluate, save or charge.
    """
    safe = observation_contract.checked(observation_contract.export, observation)
    profile = validate_requirements(requirements)
    events = [
        {
            "event": "capture_summary",
            "observation_id": safe["observation_id"],
            "capture_status": safe["capture_status"],
            "capture_summary": safe["capture_summary"],
            "execution_status": safe["execution_status"],
            "reasons": safe["reasons"],
            "duration_ms": safe["duration_ms"],
        }
    ]
    events.extend({"event": "span_completed", "span": span} for span in safe["spans"])
    gaps = (
        ["Observation gaps: " + ", ".join(safe["reasons"])] if safe["reasons"] else []
    )
    bundle = prepare_review(
        profile["objective"],
        events,
        sample=sample,
        tools=profile["tools"],
        limitations=gaps,
    )
    return prepare_submission(
        bundle,
        profile,
        observation_id=safe["observation_id"],
        inventory=classify_observation(safe),
    )


def prepare_submission(
    bundle: Mapping[str, Any],
    requirements: Mapping[str, Any],
    *,
    observation_id: str | None = None,
    inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a native-event bundle to a specific requirements revision for explicit submission.

    Args:
        bundle: Existing v1 bundle from prepare_review, revalidated with its privacy boundary.
        requirements: Reviewed v1 document; objective and tool catalog must match the bundle.
        observation_id: Optional existing obs_ ID for correlation; null for native-file imports.
        inventory: Optional classify_observation output, null for native events; descriptive only.
    Returns:
        Detached v1 submission, with a content fingerprint for immutable evaluation provenance.
    Raises:
        ValidationError: Invalid/mismatched bundle, policy or observation ID; no network occurs.
    Side Effects:
        None; owner/project identity and server credentials are never caller-controlled fields.
    """
    import json

    from .evidence.trace_tool_content import normalize_tool_content
    from .trajectory import encode_review

    profile = validate_requirements(requirements)
    safe = json.loads(encode_review(bundle))
    if safe["task"] != profile["objective"] or safe["tools"] != profile["tools"]:
        raise ValidationError(
            "Bundle task and tools must match the requirements revision"
        )
    if observation_id is not None:
        observation_contract.checked(observation_contract.identifier, observation_id)
    if inventory is not None and (
        not isinstance(inventory, Mapping)
        or inventory.get("schema_version") != "kuma.trajectory_inventory.v1"
    ):
        raise ValidationError("Trajectory inventory version is invalid")
    result, status = normalize_tool_content(
        {
            "schema_version": "kuma.trajectory_submission.v1",
            "bundle": safe,
            "requirements": profile,
            "requirements_sha256": requirements_fingerprint(profile),
            "observation_id": observation_id,
            "inventory": inventory,
        },
        redact=True,
    )
    if status != "present":
        raise ValidationError("Submission must be corrected before use")
    return result
