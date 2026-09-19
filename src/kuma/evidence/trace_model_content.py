"""Bounded observed model bodies shared by gen_ai and OpenInference capture."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .trace_tool_content import normalize_tool_content

MODEL_CONTENT_SCHEMA = "kuma.model_content.v1"
MODEL_OPERATIONS = frozenset({"chat", "text_completion"})
MODEL_STATES = frozenset(
    {
        "present",
        "redacted",
        "not_recorded",
        "sensitive_content",
        "size_limit",
        "invalid",
    }
)
_MESSAGE = re.compile(
    r"llm\.(input|output)_messages\.(0|[1-9][0-9]{0,2})\.message\."
    r"(role|content|name|tool_call_id|function_call_name|function_call_arguments_json)"
)


def model_source_keys(attributes: Mapping[str, Any]) -> set[str]:
    """Identify bounded supported body keys without treating tool proposals as execution.

    Args:
        attributes: Ended model-span attributes, already checked as a mapping.
    Returns:
        Keys owned by model-content normalization. Unknown message extensions,
        images, reasoning and arbitrary metadata remain allowlist exclusions.
    Security/Privacy:
        Only key names are examined. No value coercion, network or tool calls.
    """
    supported = {
        key
        for key in attributes
        if type(key) is str
        and (
            key
            in {
                "input.value",
                "output.value",
                "gen_ai.input.messages",
                "gen_ai.output.messages",
            }
            or _MESSAGE.fullmatch(key) is not None
        )
    }
    selected = set()
    for direction in ("input", "output"):
        native, generic = f"gen_ai.{direction}.messages", f"{direction}.value"
        if native in supported:
            selected.add(native)
        elif generic in supported:
            selected.add(generic)
        else:
            selected.update(
                key for key in supported if key.startswith(f"llm.{direction}_messages.")
            )
    return selected


def _source_value(attributes: Mapping[str, Any], direction: str) -> tuple[Any, str]:
    """Select native, generic or indexed recorded content in deterministic order.

    Missing means unknown, not an empty model response. Indexed messages retain
    their recorded indexes/fields rather than filling gaps or inventing roles.
    The finite graph/size/redaction boundary runs on the selected actual values.
    """
    for key in (f"gen_ai.{direction}.messages", f"{direction}.value"):
        if key in attributes:
            return attributes[key], "present"
    selected = {}
    for key in model_source_keys(attributes):
        match = _MESSAGE.fullmatch(key)
        if match and match[1] == direction:
            if int(match[2]) >= 128:
                return None, "size_limit"
            selected[key] = attributes[key]
    return (
        (dict(sorted(selected.items())), "present")
        if selected
        else (None, "not_recorded")
    )


def map_model_content(
    attributes: Mapping[str, Any],
) -> tuple[dict[str, Any], int, set[str]]:
    """Normalize observed model inputs/results using the existing tool body boundary.

    Args:
        attributes: Original attributes of an explicitly observed model span.
    Returns:
        Closed versioned content, count of omitted supported bodies, and stable
        loss reasons. Values exist exactly for present/redacted states, including
        a recorded JSON null. Bodies are never truncated into misleading fragments.
    Preconditions:
        The caller established a chat/text_completion operation from native or
        OpenInference semantics; a span name alone cannot establish model use.
    Postconditions:
        Each retained body is finite, detached, depth-bounded and at most 4 MiB
        canonical JSON. Existing whole-Trace, Run and request limits still apply.
    Security/Privacy:
        Shared credential redaction runs on both bodies before any persistence.
        Unknown fields remain filtered by the main mapper. No output becomes a
        tool-execution fact, Agent success, or public Judge verdict.
    """
    content: dict[str, Any] = {"schema_version": MODEL_CONTENT_SCHEMA}
    dropped = 0
    reasons = set()
    for direction in ("input", "output"):
        try:
            value, status = _source_value(attributes, direction)
            if status == "present":
                value, status = normalize_tool_content(value, redact=True)
        except Exception:
            value, status = None, "invalid"
        field = {"status": status}
        if status in {"present", "redacted"}:
            field["value"] = value
        if status != "present":
            suffix = "sensitive" if status == "sensitive_content" else status
            reasons.add(f"trace_model_content_{suffix}")
            dropped += int(status not in {"redacted", "not_recorded"})
        content[direction] = field
    return content, dropped, reasons


def validate_model_content(value: Any) -> None:
    """Require exact model-body fields/states, budgets and privacy at wire admission.

    Args:
        value: Untrusted optional normalized model-content object.
    Raises:
        ValueError: Closed shape, state/value consistency, finite graph, size or
            privacy failure. The error contains no rejected value or raw cause.
    Postconditions:
        Validation never sanitizes or silently repairs caller-supplied Evidence;
        already redacted bytes remain unchanged for artifact hash verification.
    """
    valid = False
    try:
        valid = (
            isinstance(value, Mapping)
            and set(value) == {"schema_version", "input", "output"}
            and value["schema_version"] == MODEL_CONTENT_SCHEMA
        )
        for direction in ("input", "output"):
            field = value[direction]
            status = field["status"]
            retained = status in {"present", "redacted"}
            valid = (
                valid
                and status in MODEL_STATES
                and set(field) == ({"status", "value"} if retained else {"status"})
            )
            if retained:
                # Do not parse an already normalized JSON string a second time.
                from ..repository.privacy import scan_sensitive_json
                from ..repository.tool_capabilities import _plain_json
                from .runtime_contract import (
                    RUNTIME_AGENT_OUTPUT_MAX_BYTES,
                    runtime_evidence_json,
                )

                plain = _plain_json(field["value"])
                valid = (
                    valid
                    and len(runtime_evidence_json(plain).encode())
                    <= RUNTIME_AGENT_OUTPUT_MAX_BYTES
                )
                valid = valid and not scan_sensitive_json(
                    plain, location="trace_model_content"
                )
    except Exception:
        valid = False
    if not valid:
        raise ValueError("runtime model content is invalid")


def omit_largest_model_body(spans: list[Mapping[str, Any]]) -> bool:
    """Omit one optional body deterministically, preserving all span metadata.

    Args:
        spans: Mutable detached capture/projection spans, never committed history.
    Returns:
        True if a retained model field became ``size_limit``; False if none
        remains. Largest canonical body is removed first, ties ordered by actual
        trace/span ID and field name. No fragment or synthetic value is produced.
    Postconditions:
        The caller must add one observed body omission and the stable model size
        reason, recompute its artifact hash/bytes, and mark capture partial.
    Side Effects:
        Mutates exactly one detached field; no I/O or other content is touched.
    """
    from .runtime_contract import runtime_evidence_json

    candidates = []
    for span in spans:
        content = span.get("model_content")
        if content is None:
            continue
        for direction in ("input", "output"):
            field = content[direction]
            if field["status"] in {"present", "redacted"}:
                size = len(runtime_evidence_json(field["value"]).encode("utf-8"))
                candidates.append(
                    ((-size, span["trace_id"], span["span_id"], direction), field)
                )
    if not candidates:
        return False
    field = min(candidates, key=lambda item: item[0])[1]
    field.clear()
    field["status"] = "size_limit"
    return True
