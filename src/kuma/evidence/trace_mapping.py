"""Pure, bounded conversion from ended OpenTelemetry spans to public JSON."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
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
    "authorization",
    "completion",
    "content",
    "cookie",
    "credential",
    "file.body",
    "file.content",
    "log.body",
    "private_rubric",
    "prompt",
    "source",
    "secret",
    "system_prompt",
    "token",
)
_PRIVATE_ATTRIBUTE_PATTERNS = tuple(
    tuple(term.replace(".", "_").split("_")) for term in _PRIVATE_ATTRIBUTE_TERMS
)
_ATTRIBUTE_ITERATION_ERROR = object()
_MAX_ATTRIBUTE_KEY_LENGTH = 256
_AGENT_OUTPUT_OPERATIONS = frozenset({"invoke_agent", "invoke_workflow"})
_GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"


class SpanMappingError(ValueError):
    """A stable, value-free reason why a required span field was unusable."""

    def __init__(self, reason: str) -> None:
        """Retain only a stable reason code, never the rejected span value."""
        super().__init__(reason)
        self.reason = reason


def json_size(value: Any) -> int:
    """Return the compact UTF-8 JSON size used for Evidence budgeting."""
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _safe_text(value: Any, limit: int) -> tuple[str, bool]:
    """Normalize Unicode, redact detected secrets, and truncate to the text limit."""
    text = str(value).encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    if scan_sensitive_text(text, location="trace"):
        return "[redacted]", True
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _safe_value(value: Any, limit: int) -> tuple[Any | None, bool]:
    """Map one OTel scalar or short sequence into the bounded JSON subset."""
    safe, truncated, _, _ = _safe_value_with_drops(value, limit)
    return safe, truncated


def _safe_value_with_drops(
    value: Any, limit: int
) -> tuple[Any | None, bool, int, set[str]]:
    """Map one value while counting sequence members omitted by normalization.

    This private mapper feeds the Trace attribute accounting boundary. It keeps
    at most sixteen sequence members, counts every observed member it omits, and
    never returns a rejected object's representation.

    Args:
        value: OTel attribute value to normalize.
        limit: Maximum retained characters for each text value.

    Returns:
        The safe JSON value (or ``None``), whether text/sequence data was
        truncated, the number of omitted values, and stable discard reasons.

    Security/Privacy:
        Unsupported values are counted and discarded without reading ``repr``.
    """
    if value is None or isinstance(value, bool):
        return value, False, 0, set()
    if isinstance(value, int):
        return (
            (value, False, 0, set())
            if -(2**63) <= value < 2**63
            else (None, False, 1, {"trace_attribute_invalid"})
        )
    if isinstance(value, float):
        return (
            (value, False, 0, set())
            if math.isfinite(value)
            else (None, False, 1, {"trace_attribute_invalid"})
        )
    if isinstance(value, str):
        safe, truncated = _safe_text(value, limit)
        filtered = truncated and safe == "[redacted]"
        return (
            safe,
            truncated,
            int(filtered),
            {"trace_attribute_filtered"} if filtered else set(),
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        values: list[Any] = []
        truncated = len(value) > 16
        dropped = max(0, len(value) - 16)
        reasons = {"trace_attribute_limit"} if dropped else set()
        for child in value[:16]:
            safe, child_truncated, child_dropped, child_reasons = (
                _safe_value_with_drops(child, limit)
            )
            if safe is not None:
                values.append(safe)
            else:
                child_dropped = max(1, child_dropped)
                child_reasons.add("trace_attribute_invalid")
            dropped += child_dropped
            reasons.update(child_reasons)
            truncated = truncated or child_truncated
        return values, truncated, dropped, reasons
    return None, False, 1, {"trace_attribute_invalid"}


def attribute_key_allowed(key: str, *, resource: bool = False) -> bool:
    """Allow only public resource metadata and bounded ``gen_ai`` metrics."""
    if resource:
        return key in _ALLOWED_RESOURCE_ATTRIBUTES
    return key in _ALLOWED_GEN_AI_ATTRIBUTES or key.startswith(_ALLOWED_GEN_AI_PREFIXES)


def _safe_attributes(
    attributes: Any,
    limits: TraceEvidenceLimits,
    *,
    resource: bool = False,
) -> tuple[dict[str, Any], int, bool, set[str]]:
    """Filter all observed attributes before bounding retained allowlisted values.

    ``max_attributes`` limits retained public values, not the first mapping
    entries supplied by an instrumentation library. Every observed exclusion is
    counted and receives either an allowlist, privacy-filter, limit, or invalid
    reason without retaining its key or value.
    """
    if attributes is None:
        return {}, 0, False, set()
    if not isinstance(attributes, Mapping):
        return {}, 1, True, {"trace_attribute_invalid"}
    result: dict[str, Any] = {}
    dropped = 0
    truncated = False
    reasons: set[str] = set()
    for item in _attribute_items(attributes):
        if item is _ATTRIBUTE_ITERATION_ERROR:
            dropped += 1
            truncated = True
            reasons.add("trace_attribute_invalid")
            break
        raw_key, raw_value = item
        key, value, item_truncated, item_dropped, item_reasons = _safe_attribute(
            raw_key,
            raw_value,
            limits,
            resource=resource,
            retain=len(result) < limits.max_attributes,
        )
        dropped += item_dropped
        reasons.update(item_reasons)
        if key is not None:
            result[key] = value
        if item_truncated:
            truncated = True
            if not item_reasons:
                reasons.add("trace_value_truncated")
    return result, dropped, truncated, reasons


def _attribute_items(
    attributes: Mapping[Any, Any],
) -> Iterator[tuple[Any, Any] | object]:
    """Yield mapping items and replace iterator failures with one safe sentinel.

    The caller owns discard accounting. This generator never exposes exception
    text or materializes an untrusted mapping, so memory remains bounded while
    all successfully observed items are processed.
    """
    try:
        iterator = iter(attributes.items())
    except Exception:
        yield _ATTRIBUTE_ITERATION_ERROR
        return
    while True:
        try:
            yield next(iterator)
        except StopIteration:
            return
        except Exception:
            yield _ATTRIBUTE_ITERATION_ERROR
            return


def _safe_attribute(
    raw_key: Any,
    raw_value: Any,
    limits: TraceEvidenceLimits,
    *,
    resource: bool,
    retain: bool,
) -> tuple[str | None, Any | None, bool, int, set[str]]:
    """Classify and optionally retain one attribute without exposing raw values."""
    key = _normalized_attribute_key(raw_key)
    if key is None:
        return None, None, True, 1, {"trace_attribute_invalid"}
    if not attribute_key_allowed(key, resource=resource):
        reason = (
            "trace_attribute_filtered"
            if _private_attribute_key(key)
            else "trace_attribute_not_allowlisted"
        )
        return None, None, False, 1, {reason}
    if not retain:
        return None, None, True, 1, {"trace_attribute_limit"}
    try:
        value, value_truncated, value_dropped, value_reasons = _safe_value_with_drops(
            raw_value, limits.max_text_length
        )
    except Exception:
        value, value_truncated, value_dropped, value_reasons = (
            None,
            False,
            1,
            {"trace_attribute_invalid"},
        )
    if value is None:
        value_reasons.add("trace_attribute_invalid")
        return None, None, False, max(1, value_dropped), value_reasons
    return (
        key,
        value,
        value_truncated,
        value_dropped,
        value_reasons,
    )


def _normalized_attribute_key(raw_key: Any) -> str | None:
    """Return one bounded text key without truncating standard schema names.

    ``max_text_length`` governs user-controlled values and display names, not
    OTel schema keys. Keys use a fixed internal bound so a small value limit
    cannot turn an allowlisted field into an apparent arbitrary key.
    """
    if not isinstance(raw_key, str):
        return None
    key = raw_key.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    if not key or len(key) > _MAX_ATTRIBUTE_KEY_LENGTH:
        return None
    return key


def _private_attribute_key(key: str) -> bool:
    """Classify explicit sensitive key segments without substring false positives.

    Attribute retention is still controlled solely by the strict allowlist. This
    helper only chooses the public discard reason: complete key segments such as
    ``token`` remain privacy-filtered, while benign names such as ``max_tokens``
    are reported as ordinary allowlist exclusions.
    """
    segments = tuple(
        segment
        for segment in key.casefold().replace(".", "_").replace("-", "_").split("_")
        if segment
    )
    return any(
        segments[index : index + len(pattern)] == pattern
        for pattern in _PRIVATE_ATTRIBUTE_PATTERNS
        for index in range(len(segments) - len(pattern) + 1)
    )


def _map_events(
    events: Any, limits: TraceEvidenceLimits
) -> tuple[list[dict[str, Any]], int, bool, set[str]]:
    """Map at most the configured number of events and account for omissions."""
    mapped: list[dict[str, Any]] = []
    dropped = 0
    truncated = False
    reasons: set[str] = set()
    try:
        iterator = iter(events or ())
    except Exception:
        return [], 1, True, {"trace_event_invalid"}
    index = 0
    # StopIteration or the explicit sentinel always terminates this bounded loop.
    # Writing that invariant directly avoids a synthetic, unreachable ``for``
    # exhaustion branch while preserving malformed-iterator failure isolation.
    while True:
        try:
            event = next(iterator)
        except StopIteration:
            break
        except Exception:
            dropped += 1
            truncated = True
            reasons.add("trace_event_invalid")
            break
        if index == limits.max_events_per_span:
            dropped += _event_limit_drop_count(events, index)
            truncated = True
            reasons.add("trace_event_limit")
            break
        try:
            event_data, count, event_truncated, event_reasons = _map_event(
                event, limits
            )
        except Exception:
            dropped += 1
            truncated = True
            reasons.add("trace_event_invalid")
            index += 1
            continue
        mapped.append(event_data)
        dropped += count
        truncated = truncated or event_truncated
        reasons.update(event_reasons)
        index += 1
    return mapped, dropped, truncated, reasons


def _event_limit_drop_count(events: Any, retained_count: int) -> int:
    """Count omitted events exactly when the OTel collection exposes its size.

    Generic iterators remain bounded: once the first over-limit event is
    observed, the function returns one rather than consuming an unknown or
    potentially infinite source. Standard OTel event collections are sized and
    therefore report their exact omitted count.
    """
    try:
        total = len(events)
    except (TypeError, ValueError, OverflowError):
        return 1
    return max(1, total - retained_count)


def _map_event(
    event: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, bool, set[str]]:
    """Normalize one event name, timestamp, and allowlisted attributes."""
    name, name_truncated = _safe_text(event.name, limits.max_text_length)
    attributes, dropped, truncated, reasons = _safe_attributes(
        getattr(event, "attributes", None), limits
    )
    if name_truncated:
        reasons.add("trace_value_truncated")
    return (
        {
            "name": name,
            "time_unix_nano": _required_nano(event.timestamp, "event timestamp"),
            "attributes": attributes,
        },
        dropped,
        truncated or name_truncated,
        reasons,
    )


def _required_nano(value: Any, label: str) -> int:
    """Require a non-negative integer nanosecond timestamp."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _hex_identifier(value: Any, width: int, label: str) -> str:
    """Validate an OTel numeric identifier and return fixed-width lowercase hex."""
    maximum = 1 << (width * 4)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < maximum:
        raise ValueError(f"invalid {label}")
    return f"{value:0{width}x}"


def _span_core(
    span: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], bool, set[str]]:
    """Normalize required span identity, topology, timing, kind, and status.

    Args:
        span: Ended OTel-readable span object from the provider callback.
        limits: Text/resource limits governing safe name projection.

    Returns:
        Core span mapping, whether a value was truncated, and stable degradation
        reasons for optional parent/name/enum problems.

    Raises:
        SpanMappingError: If trace/span IDs or required start/end timing are
            invalid. Optional malformed fields degrade instead of exposing text.

    Preconditions:
        The span has ended; mutable/live spans are outside exporter semantics.

    Postconditions:
        Success has fixed-width lowercase identifiers, non-negative duration,
        closed kind/status values, and a bounded name.

    Security/Privacy:
        No attributes, events, output, prompt, or exception body are read here.
    """
    try:
        context = span.context
        trace_id = _hex_identifier(context.trace_id, 32, "trace ID")
        span_id = _hex_identifier(context.span_id, 16, "span ID")
    except Exception as exc:
        raise SpanMappingError("trace_span_context_invalid") from exc
    parent = getattr(span, "parent", None)
    try:
        start = _required_nano(span.start_time, "span start")
        end = _required_nano(span.end_time, "span end")
        if end < start:
            raise ValueError("span end precedes start")
    except Exception as exc:
        raise SpanMappingError("trace_span_timing_invalid") from exc
    reasons: set[str] = set()
    try:
        name, truncated = _safe_text(span.name, limits.max_text_length)
    except Exception:
        name, truncated = "[invalid]", True
        reasons.add("trace_value_invalid")
    kind = _enum_name(getattr(span, "kind", None), "UNSPECIFIED").lower()
    status_object = getattr(span, "status", None)
    status = _enum_name(getattr(status_object, "status_code", None), "UNSET").lower()
    parent_span_id = None
    if parent is not None:
        try:
            raw_parent_id = parent.span_id
            if raw_parent_id != 0:
                parent_span_id = _hex_identifier(raw_parent_id, 16, "parent span ID")
        except Exception:
            reasons.add("trace_parent_invalid")
    return (
        {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": name,
            "kind": kind
            if kind in {"internal", "server", "client", "producer", "consumer"}
            else "unspecified",
            "status": status if status in {"unset", "ok", "error"} else "unset",
            "start_time_unix_nano": start,
            "end_time_unix_nano": end,
            "duration_nano": end - start,
        },
        truncated,
        reasons,
    )


def _enum_name(value: Any, default: str) -> str:
    """Read an enum name defensively, falling back without leaking object text."""
    try:
        name = getattr(value, "name", default)
        return name if isinstance(name, str) else default
    except Exception:
        return default


def _map_scope(
    span: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, str], bool, set[str]]:
    """Map bounded instrumentation scope name and version."""
    try:
        scope = getattr(span, "instrumentation_scope", None)
        name, name_truncated = _safe_text(
            getattr(scope, "name", ""), limits.max_text_length
        )
        version, version_truncated = _safe_text(
            getattr(scope, "version", "") or "", limits.max_text_length
        )
    except Exception:
        return {"name": "", "version": ""}, True, {"trace_scope_invalid"}
    return (
        {"name": name, "version": version},
        name_truncated or version_truncated,
        set(),
    )


def map_span(
    span: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, bool, set[str]]:
    """Convert one ended OTel span to the public privacy-filtered Evidence shape."""
    mapped, core_truncated, core_reasons = _span_core(span, limits)
    span_attributes, attribute_field_reasons = _optional_span_field(
        span, "attributes", "trace_attribute_invalid"
    )
    attributes, dropped, truncated, reasons = _safe_attributes(span_attributes, limits)
    event_source, event_field_reasons = _optional_span_field(
        span, "events", "trace_event_invalid"
    )
    events, event_dropped, event_truncated, event_reasons = _map_events(
        event_source, limits
    )
    try:
        resource_attributes = getattr(
            getattr(span, "resource", None), "attributes", None
        )
    except Exception:
        resource_attributes = None
        reasons.add("trace_attribute_invalid")
    resource, resource_dropped, resource_truncated, resource_reasons = _safe_attributes(
        resource_attributes, limits, resource=True
    )
    scope, scope_truncated, scope_reasons = _map_scope(span, limits)
    reasons.update(core_reasons)
    reasons.update(attribute_field_reasons)
    reasons.update(event_field_reasons)
    reasons.update(event_reasons)
    reasons.update(resource_reasons)
    reasons.update(scope_reasons)
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


def _optional_span_field(span: Any, name: str, reason: str) -> tuple[Any, set[str]]:
    """Read an optional span field and replace accessor failures with a reason code."""
    try:
        return getattr(span, name, None), set()
    except Exception:
        return None, {reason}


def extract_agent_output(
    span: Any, max_bytes: int
) -> tuple[tuple[int, int, int], Any] | None:
    """Return a bounded final output only from Agent/Workflow semantic spans."""

    try:
        return _extract_agent_output(span, max_bytes)
    except Exception:
        return None


def _extract_agent_output(
    span: Any, max_bytes: int
) -> tuple[tuple[int, int, int], Any] | None:
    """Extract actual semantic-convention Agent output with deterministic priority."""
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
    return (priority, end_time, span_id), output


def _parse_output_messages(value: Any, max_bytes: int) -> list[dict[str, Any]] | None:
    """Accept only a bounded JSON list of output-message objects."""
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


__all__ = [
    "SpanMappingError",
    "attribute_key_allowed",
    "extract_agent_output",
    "json_size",
    "map_span",
]
