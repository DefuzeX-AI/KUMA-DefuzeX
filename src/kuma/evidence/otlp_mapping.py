"""Decode OTLP Protobuf into the existing privacy-filtered span mapper's inputs."""

from types import SimpleNamespace
from typing import Any

from .trace_mapping import SpanMappingError

_KINDS = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}
_STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}


def _value(value: Any, depth: int = 0) -> Any:
    """Decode bounded OTLP values without evaluating or coercing their content."""
    if depth > 16:
        raise SpanMappingError("trace_attribute_invalid")
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [_value(item, depth + 1) for item in value.array_value.values]
    if kind == "kvlist_value":
        return _attributes(value.kvlist_value.values, depth + 1)
    if kind in {"string_value", "bool_value", "int_value", "double_value"}:
        return getattr(value, kind)
    return None


def _attributes(items: Any, depth: int = 0) -> dict[str, Any]:
    """Reject duplicate OTLP keys instead of silently choosing one observed value."""
    result = {}
    for item in items:
        if item.key in result:
            raise SpanMappingError("trace_attribute_invalid")
        result[item.key] = _value(item.value, depth)
    return result


def _context(trace_id: bytes, span_id: bytes) -> SimpleNamespace:
    """Require exact nonzero OTLP identity widths before converting to OTel integers."""
    if (
        len(trace_id) != 16
        or len(span_id) != 8
        or not any(trace_id)
        or not any(span_id)
    ):
        raise SpanMappingError("trace_span_context_invalid")
    return SimpleNamespace(
        trace_id=int.from_bytes(trace_id, "big"), span_id=int.from_bytes(span_id, "big")
    )


def readable_span(span: Any, resource: Any, scope: Any) -> SimpleNamespace:
    """Adapt one decoded OTLP span for TraceEvidenceCapture's normal mapping boundary.

    No data is retained here. The caller passes the result through the same metadata
    allowlist, model/tool body redaction, sampling and aggregate limits as Python spans.
    Invalid identities, enums or duplicate attributes raise a value-free mapping error.
    """
    if span.kind not in _KINDS or span.status.code not in _STATUS:
        raise SpanMappingError("trace_value_invalid")
    if span.end_time_unix_nano < span.start_time_unix_nano:
        raise SpanMappingError("trace_span_timing_invalid")
    parent = None
    if span.parent_span_id:
        parent = _context(span.trace_id, span.parent_span_id)
    return SimpleNamespace(
        context=_context(span.trace_id, span.span_id),
        parent=parent,
        name=span.name,
        kind=SimpleNamespace(name=_KINDS[span.kind]),
        status=SimpleNamespace(
            status_code=SimpleNamespace(name=_STATUS[span.status.code])
        ),
        start_time=span.start_time_unix_nano,
        end_time=span.end_time_unix_nano,
        attributes=_attributes(span.attributes),
        events=[
            SimpleNamespace(
                name=e.name,
                timestamp=e.time_unix_nano,
                attributes=_attributes(e.attributes),
                dropped_attributes=e.dropped_attributes_count,
            )
            for e in span.events
        ],
        links=[
            SimpleNamespace(context=_context(link.trace_id, link.span_id))
            for link in span.links
        ],
        resource=SimpleNamespace(attributes=_attributes(resource.attributes)),
        instrumentation_scope=SimpleNamespace(name=scope.name, version=scope.version),
        dropped_attributes=(
            span.dropped_attributes_count
            + resource.dropped_attributes_count
            + scope.dropped_attributes_count
            + sum(event.dropped_attributes_count for event in span.events)
        ),
        dropped_events=span.dropped_events_count,
        dropped_links=span.dropped_links_count,
    )
