"""Portable ATIF source partitioning for the core SDK; no Agent/framework execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .capture_privacy import redact_capture_json
from .errors import ValidationError
from .evidence.trace_tool_content import normalize_tool_content
from .trajectory import prepare_review

ATIF_VERSIONS = ("ATIF-v1.7", "ATIF-v1.8")


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
    """Partition common ATIF 1.7/1.8 input into sanitized source-addressed records.

    Args:
        document: Caller-owned ATIF 1.7/1.8 JSON with optional root/session IDs,
            ordered steps and optional nested subagent_trajectories. Embedded
            child IDs must be nonempty and unique within their parent's list.
    Returns:
        Detached event list preserving every source field. JSON pointers address original nodes;
        event order is source traversal, not invented global execution chronology. User-step
        boundaries supply explicitly derived turn IDs within each execution only.
        Missing root IDs or cross-scope repeated IDs use deterministic coordinates
        with trajectory_id_origin=source_pointer_sha256, never recorded identities.
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
    clean, _ = redact_capture_json(clean)
    digest = _digest(clean)
    layout, derived = _layout(clean)
    coordinates = {
        pointer: "atif-derived:" + _digest(["kuma.atif.coordinate.v1", digest, pointer])
        if derived
        else node["trajectory_id"]
        for node, pointer, _ in layout
    }
    events: list[dict[str, Any]] = []
    for node, pointer, parent_pointer in layout:
        identity = coordinates[pointer]
        parent = coordinates[parent_pointer] if parent_pointer is not None else None
        children = node.get("subagent_trajectories")
        meta = {
            k: v for k, v in node.items() if k not in {"steps", "subagent_trajectories"}
        }
        base = {
            "source_format": node["schema_version"],
            "source_sha256": digest,
            "trajectory_id": identity,
            "parent_trajectory_id": parent,
        }
        if derived:
            base["trajectory_id_origin"] = "source_pointer_sha256"
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
    return events


def _layout(document: dict) -> tuple[list[tuple[dict, str, str | None]], bool]:
    """Validate source-local identities and select one unambiguous coordinate mode.

    Called after finite-JSON/privacy preparation and before partitioning. Returns
    preorder nodes with original and parent pointers plus whether all envelope
    coordinates must be derived. Root absence/null or global repetition selects
    derived mode; recorded metadata is never filled or changed. Sibling duplicates,
    missing embedded IDs and more than 10000 records raise safe ValidationError.
    No references are resolved and no I/O or caller mutation occurs.
    """
    pending = [(document, "", None)]
    layout, seen = [], set()
    derived, records = False, 0
    while pending:
        node, pointer, parent = pending.pop()
        _node(node, embedded=parent is not None)
        identity = node.get("trajectory_id")
        derived = derived or identity is None or identity in seen
        seen.add(identity)
        layout.append((node, pointer, parent))
        records += 1 + len(node["steps"])
        children = node.get("subagent_trajectories") or []
        sibling_ids = []
        for child in children:
            _node(child, embedded=True)
            sibling_ids.append(child["trajectory_id"])
        if len(set(sibling_ids)) != len(sibling_ids):
            raise ValidationError("ATIF sibling trajectory IDs must be unique")
        pending.extend(
            (children[i], pointer + "/subagent_trajectories/" + str(i), pointer)
            for i in range(len(children) - 1, -1, -1)
        )
        if records + len(pending) > 10000:
            raise ValidationError("ATIF partition exceeds 10000 records")
    return layout, derived


def _node(node: Any, *, embedded: bool) -> None:
    """Validate one recorded node without inventing session or execution identity.

    The partitioner's layout pass supplies embedded=True only for actual children.
    Optional IDs retain absence/null; trajectory IDs must be bounded nonblank text.
    Session metadata retains any recorded string, including empty/whitespace text.
    Common 1.7/1.8 metadata and unique positive integer steps are required.
    Raises safe ValidationError on malformed source, with no mutation or I/O.
    """
    if not isinstance(node, dict) or node.get("schema_version") not in ATIF_VERSIONS:
        raise ValidationError("Only ATIF-v1.7 and ATIF-v1.8 trajectories are supported")
    identity = node.get("trajectory_id")
    if (identity is None and embedded) or (
        identity is not None
        and (
            not isinstance(identity, str) or not identity.strip() or len(identity) > 256
        )
    ):
        raise ValidationError("ATIF trajectory ID is invalid or missing on a child")
    session = node.get("session_id")
    if not isinstance(node.get("agent"), dict) or (
        session is not None and not isinstance(session, str)
    ):
        raise ValidationError("ATIF agent or optional session metadata is invalid")
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
        document: ATIF 1.7/1.8 source object, bounded by existing four-MiB/depth-32 preparation.
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
    clean, _ = redact_capture_json(clean)
    if clean != dict(document):
        result["content_status"] = "redacted"
    return result
