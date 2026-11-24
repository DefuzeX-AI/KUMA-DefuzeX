"""Same-callback LangChain provenance without mutating vendor spans or exporters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any

_CURRENT: ContextVar[Any] = ContextVar("kuma_langchain_source", default=None)
_UNBOUND = object()
_WRAPPER = object()
_TYPES = frozenset({"llm", "tool", "chain", "retriever", "unknown"})
_KINDS = frozenset({"LLM", "TOOL", "CHAIN", "AGENT", "RETRIEVER", "unknown"})
_MAPPING = {"llm": "chat", "tool": "execute_tool", "chain": "invoke_workflow"}
_VENDOR = {
    "LLM": "chat",
    "TOOL": "execute_tool",
    "CHAIN": "invoke_workflow",
    "AGENT": "invoke_agent",
}


def attach_langchain_source(instrumentor: Any) -> None:
    """Bind only a newly KUMA-owned optional tracer's synchronous end callback.

    Args:
        instrumentor: Successfully installed KUMA-owned LangChain instrumentor;
            external pre-existing instrumentation must never be passed here.
    Raises:
        ValueError: The supported callback/get-span boundary is unavailable.
    Side Effects:
        Wrap this tracer instance once. During its original end callback only,
        expose the actual run type paired with the exact trace/span identity to
        KUMA's mapper. Finally reset context on success or failure. No vendor
        span, global tracer class, exporter, Agent input or callback result changes.
        Missing lookup identity instead marks a window-level unknown-type gap;
        no per-span provenance or model identity is fabricated.
    Security/Privacy:
        Only closed type labels and numeric span identity enter the context.
        This is observation provenance, not authenticated proof of execution.
    """
    tracer = getattr(instrumentor, "_tracer", None)
    original = getattr(tracer, "_end_trace", None)
    lookup = getattr(tracer, "get_span", None)
    if not callable(original) or not callable(lookup):
        raise ValueError("LangChain callback source unavailable")
    if getattr(original, "_kuma_source_wrapper", None) is _WRAPPER:
        return

    def end(run: Any) -> Any:
        """Scope provenance to the original synchronous same-run span finalization."""
        source = _UNBOUND
        try:
            span = lookup(run.id)
            context = span.get_span_context()
            run_type = run.run_type
            source = (
                context.trace_id,
                context.span_id,
                run_type if type(run_type) is str and run_type in _TYPES else "unknown",
            )
        except Exception:
            pass  # Mark a window-level gap; never guess which span belongs to this run.
        token = _CURRENT.set(source)
        try:
            return original(run)
        finally:
            _CURRENT.reset(token)

    end._kuma_source_wrapper = _WRAPPER
    tracer._end_trace = end


def project_source(
    span: Any, attributes: Mapping[str, Any]
) -> tuple[Any, dict[str, str] | None, set[str]]:
    """Project same-callback identity into detached KUMA attributes and provenance.

    Native operation, including unknown values, always wins. Without native
    identity only known callback types map to operations; retriever/unknown do
    not become model/tool. Vendor kind is preserved as a closed original label.
    Unrelated/external spans return their attributes unchanged. No span is edited.
    """
    current = _CURRENT.get()
    if current is None:
        return attributes, None, set()
    if current is _UNBOUND:
        return attributes, None, {"trace_operation_type_unknown"}
    context = span.get_span_context()
    if (context.trace_id, context.span_id) != current[:2]:
        return attributes, None, set()
    kind = attributes.get("openinference.span.kind")
    source = {
        "source": "langchain_callback",
        "run_type": current[2],
        "openinference_kind": kind
        if type(kind) is str and kind in _KINDS
        else "unknown",
    }
    result = dict(attributes)
    expected = _MAPPING.get(current[2])
    reasons = set()
    if "gen_ai.operation.name" in result:
        if expected is None or result["gen_ai.operation.name"] != expected:
            reasons.add("trace_operation_type_conflict")
    elif expected is not None:
        result["gen_ai.operation.name"] = expected
    else:
        result.pop("openinference.span.kind", None)
    if expected is None or source["openinference_kind"] == "unknown":
        reasons.add("trace_operation_type_unknown")
    if expected is not None and _VENDOR.get(source["openinference_kind"]) != expected:
        reasons.add("trace_operation_type_conflict")
    return result, source, reasons


def validate_source(
    span: Mapping[str, Any], reasons: Sequence[str] | None = None
) -> None:
    """Validate optional closed provenance and diagnostic consistency at admission.

    Omission preserves old packages. Call once for shape, then with envelope
    reasons for mapping checks. A conflict permits retained native identity;
    without a native-present bit it cannot prove execution or identify its source.
    Vendor kind is descriptive at admission, not a requirement for extra reasons
    when a known callback already agrees with the recorded wire operation.
    Raises value-free ValueError on malformed fields or an unexplained mismatch.
    """
    if "operation_source" not in span:
        return
    source = span["operation_source"]
    if not isinstance(source, Mapping) or set(source) != {
        "source",
        "run_type",
        "openinference_kind",
    }:
        raise ValueError("Invalid operation source")
    if (
        source["source"] != "langchain_callback"
        or type(source["run_type"]) is not str
        or source["run_type"] not in _TYPES
        or type(source["openinference_kind"]) is not str
        or source["openinference_kind"] not in _KINDS
    ):
        raise ValueError("Invalid operation source")
    if reasons is None:
        return
    expected = _MAPPING.get(source["run_type"])
    unknown = expected is None
    conflict = expected is not None and (
        span["attributes"].get("gen_ai.operation.name") != expected
    )
    if unknown and "trace_operation_type_unknown" not in reasons:
        raise ValueError("Missing operation source diagnostic")
    if expected is None and "gen_ai.operation.name" in span["attributes"]:
        conflict = True
    if conflict and "trace_operation_type_conflict" not in reasons:
        raise ValueError("Missing operation source diagnostic")
