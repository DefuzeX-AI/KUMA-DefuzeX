"""Pure, bounded conversion from ended OpenTelemetry spans to public JSON."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import TYPE_CHECKING, Any

from ..repository.privacy import scan_sensitive_text

if TYPE_CHECKING:
    from .trace import TraceEvidenceLimits

_ALLOWED_GEN_AI_ATTRIBUTES = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.system",
    }
)
_ALLOWED_GEN_AI_PREFIXES = (
    "gen_ai.latency.",
    "gen_ai.token.usage.",
    "gen_ai.usage.",
)
_ALLOWED_RESOURCE_ATTRIBUTES = frozenset(
    {
        "deployment.environment.name",
        "service.name",
        "service.version",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    }
)
_PRIVATE_ATTRIBUTE_TERMS = (
    "api_key",
    "completion",
    "content",
    "file.body",
    "file.content",
    "log.body",
    "private_rubric",
    "prompt",
    "source",
    "system_prompt",
    "token",
)
_AGENT_OUTPUT_OPERATIONS = frozenset({"invoke_agent", "invoke_workflow"})
_GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"


def json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _safe_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value).encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    if scan_sensitive_text(text, location="trace"):
        return "[redacted]", True
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _safe_value(value: Any, limit: int) -> tuple[Any | None, bool]:
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return (value, False) if -(2**63) <= value < 2**63 else (None, False)
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, False)
    if isinstance(value, str):
        return _safe_text(value, limit)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        values: list[Any] = []
        truncated = len(value) > 16
        for child in value[:16]:
            safe, child_truncated = _safe_value(child, limit)
            if safe is not None:
                values.append(safe)
            truncated = truncated or child_truncated
        return values, truncated
    return None, False


def attribute_key_allowed(key: str, *, resource: bool = False) -> bool:
    if resource:
        return key in _ALLOWED_RESOURCE_ATTRIBUTES
    return key in _ALLOWED_GEN_AI_ATTRIBUTES or key.startswith(_ALLOWED_GEN_AI_PREFIXES)


def _safe_attributes(
    attributes: Any,
    limits: TraceEvidenceLimits,
    *,
    resource: bool = False,
) -> tuple[dict[str, Any], int, bool, set[str]]:
    if not isinstance(attributes, Mapping):
        return {}, 0, False, set()
    result: dict[str, Any] = {}
    dropped = 0
    truncated = False
    reasons: set[str] = set()
    inspected = list(islice(attributes.items(), limits.max_attributes + 1))
    if len(inspected) > limits.max_attributes:
        inspected.pop()
        dropped += 1
        truncated = True
        reasons.add("trace_attribute_limit")
    for raw_key, raw_value in inspected:
        key, key_truncated = _safe_text(raw_key, limits.max_text_length)
        allowed = attribute_key_allowed(key, resource=resource)
        if not allowed:
            if any(term in key.casefold() for term in _PRIVATE_ATTRIBUTE_TERMS):
                dropped += 1
                reasons.add("trace_attribute_filtered")
            continue
        value, value_truncated = _safe_value(raw_value, limits.max_text_length)
        if value is None:
            dropped += 1
            reasons.add("trace_attribute_invalid")
            continue
        result[key] = value
        if key_truncated or value_truncated:
            truncated = True
            reasons.add("trace_value_truncated")
    return result, dropped, truncated, reasons


def _map_events(
    events: Any, limits: TraceEvidenceLimits
) -> tuple[list[dict[str, Any]], int, bool, set[str]]:
    source = list(islice(events or (), limits.max_events_per_span + 1))
    dropped = int(len(source) > limits.max_events_per_span)
    if dropped:
        source.pop()
    mapped: list[dict[str, Any]] = []
    truncated = bool(dropped)
    reasons = {"trace_event_limit"} if dropped else set()
    for event in source:
        name, name_truncated = _safe_text(event.name, limits.max_text_length)
        attributes, count, attr_truncated, attr_reasons = _safe_attributes(
            getattr(event, "attributes", None), limits
        )
        mapped.append(
            {
                "name": name,
                "time_unix_nano": _required_nano(event.timestamp, "event timestamp"),
                "attributes": attributes,
            }
        )
        dropped += count
        truncated = truncated or name_truncated or attr_truncated
        reasons.update(attr_reasons)
        if name_truncated:
            reasons.add("trace_value_truncated")
    return mapped, dropped, truncated, reasons


def _required_nano(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _hex_identifier(value: Any, width: int, label: str) -> str:
    maximum = 1 << (width * 4)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < maximum:
        raise ValueError(f"invalid {label}")
    return f"{value:0{width}x}"


def _span_core(span: Any, limits: TraceEvidenceLimits) -> tuple[dict[str, Any], bool]:
    context = span.context
    parent = getattr(span, "parent", None)
    start = _required_nano(span.start_time, "span start")
    end = _required_nano(span.end_time, "span end")
    if end < start:
        raise ValueError("span end precedes start")
    name, truncated = _safe_text(span.name, limits.max_text_length)
    kind = getattr(getattr(span, "kind", None), "name", "UNSPECIFIED").lower()
    status = getattr(
        getattr(getattr(span, "status", None), "status_code", None),
        "name",
        "UNSET",
    ).lower()
    return {
        "trace_id": _hex_identifier(context.trace_id, 32, "trace ID"),
        "span_id": _hex_identifier(context.span_id, 16, "span ID"),
        "parent_span_id": (
            None
            if parent is None
            else _hex_identifier(parent.span_id, 16, "parent span ID")
        ),
        "name": name,
        "kind": kind
        if kind in {"internal", "server", "client", "producer", "consumer"}
        else "unspecified",
        "status": status if status in {"unset", "ok", "error"} else "unset",
        "start_time_unix_nano": start,
        "end_time_unix_nano": end,
        "duration_nano": end - start,
    }, truncated


def _map_scope(span: Any, limits: TraceEvidenceLimits) -> tuple[dict[str, str], bool]:
    scope = getattr(span, "instrumentation_scope", None)
    name, name_truncated = _safe_text(
        getattr(scope, "name", ""), limits.max_text_length
    )
    version, version_truncated = _safe_text(
        getattr(scope, "version", "") or "", limits.max_text_length
    )
    return {"name": name, "version": version}, name_truncated or version_truncated


def map_span(
    span: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, bool, set[str]]:
    mapped, core_truncated = _span_core(span, limits)
    attributes, dropped, truncated, reasons = _safe_attributes(
        getattr(span, "attributes", None), limits
    )
    events, event_dropped, event_truncated, event_reasons = _map_events(
        getattr(span, "events", None), limits
    )
    resource, resource_dropped, resource_truncated, resource_reasons = _safe_attributes(
        getattr(getattr(span, "resource", None), "attributes", None),
        limits,
        resource=True,
    )
    scope, scope_truncated = _map_scope(span, limits)
    reasons.update(event_reasons)
    reasons.update(resource_reasons)
    if core_truncated or scope_truncated:
        reasons.add("trace_value_truncated")
    mapped.update(
        attributes=attributes,
        events=events,
        resource=resource,
        scope=scope,
    )
    return (
        mapped,
        dropped + event_dropped + resource_dropped,
        truncated
        or event_truncated
        or resource_truncated
        or core_truncated
        or scope_truncated,
        reasons,
    )


def extract_agent_output(
    span: Any, max_bytes: int
) -> tuple[tuple[int, int, int], Any] | None:
    """Return a bounded final output only from Agent/Workflow semantic spans."""

    attributes = getattr(span, "attributes", None)
    if not isinstance(attributes, Mapping):
        return None
    operation = attributes.get("gen_ai.operation.name")
    if operation not in _AGENT_OUTPUT_OPERATIONS:
        return None
    raw_output = attributes.get(_GEN_AI_OUTPUT_MESSAGES)
    for event in getattr(span, "events", None) or ():
        event_attributes = getattr(event, "attributes", None)
        if (
            isinstance(event_attributes, Mapping)
            and _GEN_AI_OUTPUT_MESSAGES in event_attributes
        ):
            raw_output = event_attributes[_GEN_AI_OUTPUT_MESSAGES]
    output = _parse_output_messages(raw_output, max_bytes)
    if output is None:
        return None
    end_time = getattr(span, "end_time", None)
    span_id = getattr(getattr(span, "context", None), "span_id", None)
    if (
        isinstance(end_time, bool)
        or not isinstance(end_time, int)
        or end_time < 0
        or isinstance(span_id, bool)
        or not isinstance(span_id, int)
    ):
        return None
    priority = 1 if operation == "invoke_workflow" else 0
    return (end_time, priority, span_id), output


def _parse_output_messages(value: Any, max_bytes: int) -> list[dict[str, Any]] | None:
    if isinstance(value, str):
        if len(value.encode("utf-8", "surrogatepass")) > max_bytes:
            return None
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    elif isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or not value:
        return None
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        normalized = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if len(encoded) > max_bytes or not all(
        isinstance(message, dict) for message in normalized
    ):
        return None
    return normalized


__all__ = ["attribute_key_allowed", "extract_agent_output", "json_size", "map_span"]
