"""Portable ATIF source partitioning for the core SDK; no Agent/framework execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError
from .evidence.trace_tool_content import normalize_tool_content
from .trajectory import prepare_review

ATIF_VERSION = "ATIF-v1.8"


def _digest(value: Any) -> str:
    """Bind source content with finite canonical JSON while preserving integer precision."""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def atif_events(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Partition a finite ATIF 1.8 document into lossless, source-addressed records.

    Args:
        document: Caller-owned JSON trajectory with unique trajectory IDs, ordered steps and
            optional nested subagent_trajectories. Unknown fields and artifact references stay data.
    Returns:
        Detached event list preserving every source field. JSON pointers address original nodes;
        event order is source traversal, not invented global execution chronology. User-step
        boundaries supply explicitly derived turn IDs within each execution only.
    Raises:
        ValidationError: Invalid, oversized, nonfinite/deep, sensitive or ambiguous source.
    Side Effects:
        None. No file/URL resolution, imports of Agent code, network or model calls.
    """
    if not isinstance(document, Mapping):
        raise ValidationError("ATIF document must be an object")
    clean, status = normalize_tool_content(dict(document), redact=True)
    if status not in {"present", "redacted"} or not isinstance(clean, dict):
        raise ValidationError("ATIF source cannot be safely prepared")
    digest = _digest(clean)
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    pending = [(clean, "", None)]
    while pending:
        node, pointer, parent = pending.pop()
        _node(node, seen)
        identity = node["trajectory_id"]
        children = node.get("subagent_trajectories")
        meta = {
            k: v for k, v in node.items() if k not in {"steps", "subagent_trajectories"}
        }
        base = {
            "source_format": ATIF_VERSION,
            "source_sha256": digest,
            "trajectory_id": identity,
            "parent_trajectory_id": parent,
        }
        events.append(
            {
                **base,
                "event": "atif.trajectory",
                "source_pointer": pointer,
                "children_state": "absent"
                if "subagent_trajectories" not in node
                else "null"
                if children is None
                else "list",
                "record": meta,
                "turn_id": None,
            }
        )
        turn = None
        for index, step in enumerate(node["steps"]):
            if step["source"] == "user":
                turn = identity + ":user-step:" + str(step["step_id"])
            events.append(
                {
                    **base,
                    "event": "atif.step",
                    "source_pointer": pointer + "/steps/" + str(index),
                    "record": step,
                    "turn_id": turn,
                    "turn_origin": "ordered_atif_user_step" if turn else None,
                }
            )
        for index in range(len(children or []) - 1, -1, -1):
            pending.append(
                (
                    children[index],
                    pointer + "/subagent_trajectories/" + str(index),
                    identity,
                )
            )
        if len(events) + len(pending) > 10000:
            raise ValidationError("ATIF partition exceeds 10000 records")
    return events


def _node(node: Any, seen: set[str]) -> None:
    """Reject ambiguous execution/step coordinates without interpreting captured content."""
    if not isinstance(node, dict) or node.get("schema_version") != ATIF_VERSION:
        raise ValidationError("Only ATIF-v1.8 trajectory objects are supported")
    identity = node.get("trajectory_id")
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or len(identity) > 256
        or identity in seen
    ):
        raise ValidationError("ATIF trajectory identities must be unique nonempty text")
    seen.add(identity)
    if not isinstance(node.get("agent"), dict) or not isinstance(
        node.get("session_id"), str
    ):
        raise ValidationError("ATIF agent and session metadata are required")
    steps, children = node.get("steps"), node.get("subagent_trajectories")
    if (
        not isinstance(steps, list)
        or not steps
        or (children is not None and not isinstance(children, list))
    ):
        raise ValidationError(
            "ATIF requires recorded steps and optional child trajectories"
        )
    ids = set()
    for step in steps:
        if (
            not isinstance(step, dict)
            or type(step.get("step_id")) is not int
            or step["step_id"] < 1
        ):
            raise ValidationError("ATIF step IDs must be positive integers")
        role = step.get("source")
        if (
            step["step_id"] in ids
            or not isinstance(role, str)
            or role not in {"system", "user", "agent"}
        ):
            raise ValidationError("ATIF step identities or roles are invalid")
        ids.add(step["step_id"])


def prepare_atif(
    task: str,
    document: Mapping[str, Any],
    *,
    sample: str = "trajectory",
    tools: list[dict[str, Any]] | None = None,
    limitations: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Prepare ATIF as a normal public review bundle with exact source coordinates.

    Args:
        task: Actual caller-provided task/review scope, 1..12000 characters; never inferred.
        document: ATIF-v1.8 source object, bounded by existing four-MiB/depth-32 preparation.
        sample: Nonblank review label up to 200 characters, default trajectory.
        tools: Actual declared tools, None for unknown, empty list for explicitly no tools.
        limitations: Up to 32 caller-known capture limits. Format notes use remaining slots;
            Core independently preserves source-format and coordinate semantics.
    Returns:
        Existing v1 bundle usable with prepare_submission/upload, retaining redaction status.
        Source hash covers the sanitized original document, not unavailable raw private bytes.
    Raises:
        ValidationError: Unsafe/invalid source, policy or existing bundle size limits exceeded.
    Side Effects:
        None; references and scripts are never opened, fetched or executed. Caller owns submission.
    """
    if not isinstance(limitations, (list, tuple)) or len(limitations) > 32:
        raise ValidationError("ATIF preparation accepts at most 32 caller limitations")
    events = atif_events(document)
    notes = [
        "ATIF record order is source traversal; timestamps do not imply cross-Agent causality.",
        "External artifact references are preserved, never automatically resolved or executed.",
    ]
    result = prepare_review(
        task,
        events,
        sample=sample,
        tools=tools,
        limitations=[*limitations, *notes[: 32 - len(limitations)]],
    )
    clean, _ = normalize_tool_content(dict(document), redact=True)
    if clean != dict(document):
        result["content_status"] = "redacted"
    return result
