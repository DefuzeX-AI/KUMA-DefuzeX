"""Optional local trajectory-review preparation; no model, credentials or network access."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .capture_privacy import redact_capture_json
from .errors import ValidationError
from .evidence.trace_tool_content import normalize_tool_content

SCHEMA_VERSION = "kuma.trajectory_review_bundle.v1"
MAX_EVENTS = 10_000
MAX_TOOLS = 256


def prepare_review(
    task: str,
    events: Sequence[Mapping[str, Any]],
    *,
    sample: str = "trajectory",
    tools: Sequence[Mapping[str, Any]] | None = None,
    limitations: Sequence[str] = (),
) -> dict[str, Any]:
    """Detach and redact existing task/tool/event records for local Core review.

    Args:
        task: Required original task text, 1..12000 characters after trimming.
            Preserve amendments in the text or recorded events; do not add a rubric.
        events: Ordered exported event objects, 1..10000 entries. Each requires
            a nonempty string ``event`` field. Preserve observed fields, including
            tool, arguments, result, ok, turn_id and operation IDs when available.
            This helper neither collects telemetry nor parses vendor log files.
        sample: Nonempty review label, at most 200 characters; default trajectory.
        tools: Actual tools available during execution, at most 256 objects with
            distinct nonempty ``name`` fields. Include descriptions, parameter
            schemas, prerequisites and availability changes when known. None means
            not supplied; an empty list explicitly declares no available tools.
        limitations: At most 32 known capture gaps as nonempty strings. Missing
            outputs, changed tool versions and partial logs belong here.
    Returns:
        Detached ``kuma.trajectory_review_bundle.v1`` JSON. ``content_status``
        distinguishes present from redacted content; the original is unchanged.
    Raises:
        ValidationError: Invalid shape, graph, size or remaining sensitive content.
            Diagnostics never include source values. Nothing is silently truncated.
    Side Effects:
        None. No filesystem, environment, API key, model or network access.
    Security/Privacy:
        Reuses KUMA's existing finite-JSON and Evidence redaction/scanning boundary,
        capped at 4 MiB and depth 32. There is no allow-sensitive override.
        Detection covers recognized patterns, not all possible private information.
        Review-specific projection also redacts known email, IPv4 and user-home
        spellings without changing general Case/Run Evidence privacy policy.
        Review prepared contents before sharing them outside your environment.
    """
    _text(task, 12_000)
    _text(sample, 200)
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= MAX_EVENTS:
        raise ValidationError("Trajectory events must contain 1..10000 objects")
    for event in events:
        if not isinstance(event, Mapping):
            raise ValidationError("Trajectory events require JSON objects")
        _text(event.get("event"), 120)
    _tools(tools)
    if not isinstance(limitations, (list, tuple)) or len(limitations) > 32:
        raise ValidationError("Trajectory limitations must contain at most 32 items")
    for item in limitations:
        _text(item, 1500)
    payload, status = normalize_tool_content(
        {
            "sample": sample,
            "task": task,
            "events": list(events),
            "tools": list(tools) if tools is not None else None,
            "limitations": list(limitations),
        },
        redact=True,
    )
    if status not in {"present", "redacted"}:
        raise ValidationError("Trajectory content could not be safely prepared")
    payload, pii_changed = redact_capture_json(payload)
    if pii_changed:
        status = "redacted"
    bundle = {"schema_version": SCHEMA_VERSION, "content_status": status, **payload}
    _canonical(bundle)
    return bundle


def _canonical(value: Mapping[str, Any]) -> bytes:
    """Encode validated JSON under the complete bundle bound with stable fingerprint ordering."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 4 * 1024 * 1024:
        raise ValidationError("Trajectory bundle exceeds the 4 MiB limit")
    return encoded


def _text(value: Any, maximum: int) -> None:
    """Reject invalid bounded text without printing the supplied value."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError("Trajectory text is missing or exceeds its limit")


def _tools(tools: Sequence[Mapping[str, Any]] | None) -> None:
    """Validate supplied availability descriptions without inventing tool capabilities."""
    if tools is None:
        return
    if not isinstance(tools, (list, tuple)) or len(tools) > MAX_TOOLS:
        raise ValidationError("Trajectory tools must contain at most 256 objects")
    names = set()
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise ValidationError("Trajectory tools require JSON objects")
        name = tool.get("name")
        _text(name, 200)
        if name in names:
            raise ValidationError("Trajectory tools require unique names")
        names.add(name)


def encode_review(bundle: Mapping[str, Any]) -> bytes:
    """Revalidate and serialize a prepared bundle as canonical UTF-8 JSON bytes.

    Args:
        bundle: A prepared v1 bundle. Content is checked and sanitized again so
            edits made after preparation cannot bypass the Evidence boundary.
    Returns:
        Sorted, compact UTF-8 JSON suitable for a caller-owned local file.
        Redacted status is retained when revalidation makes no further changes.
    Raises:
        ValidationError: Unsupported schema, fields or content. No raw value is echoed.
    Side Effects:
        None; the caller owns file creation and any later submission authorization.
        This is a local preparation API, not a live hosted review endpoint.
    """
    allowed = {
        "schema_version",
        "content_status",
        "sample",
        "task",
        "events",
        "tools",
        "limitations",
    }
    if not isinstance(bundle, Mapping) or set(bundle) != allowed:
        raise ValidationError("Trajectory bundle fields are invalid")
    if bundle["schema_version"] != SCHEMA_VERSION or bundle["content_status"] not in {
        "present",
        "redacted",
    }:
        raise ValidationError("Trajectory bundle version or content status is invalid")
    safe = prepare_review(
        bundle["task"],
        bundle["events"],
        sample=bundle["sample"],
        tools=bundle["tools"],
        limitations=bundle["limitations"],
    )
    if bundle["content_status"] == "redacted":
        safe["content_status"] = "redacted"
    return _canonical(safe)


__all__ = ["SCHEMA_VERSION", "encode_review", "prepare_review"]
