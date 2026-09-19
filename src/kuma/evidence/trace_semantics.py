"""Translate observed OpenInference attributes into the existing OTel mapper.

This is a semantic-key projection, not a framework adapter or a second capture
pipeline. Unknown fields still pass through the existing allowlist accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_KINDS = {
    "TOOL": "execute_tool",
    "LLM": "chat",
    "AGENT": "invoke_agent",
    "CHAIN": "invoke_workflow",
}
_METADATA = {
    "llm.model_name": "gen_ai.request.model",
    "llm.provider": "gen_ai.provider.name",
    "llm.system": "gen_ai.system",
    "llm.token_count.prompt": "gen_ai.usage.input_tokens",
    "llm.token_count.completion": "gen_ai.usage.output_tokens",
    "llm.token_count.total": "gen_ai.usage.total_tokens",
}
_TOOL_FIELDS = {
    "tool.name": "gen_ai.tool.name",
    "input.value": "gen_ai.tool.call.arguments",
    "output.value": "gen_ai.tool.call.result",
}


def observed_operation(attributes: Any) -> str | None:
    """Resolve an explicitly recorded operation without inferring execution.

    Args:
        attributes: Original ended-span attributes from OTel instrumentation.
    Returns:
        The explicit gen_ai operation, or a recognized OpenInference kind's
        equivalent operation. Invalid or missing identity returns ``None``.
    Postconditions:
        Native gen_ai identity takes precedence, including unknown values;
        a contradictory OpenInference kind cannot upgrade it to a tool call.
    Security/Privacy:
        No span name, tool suggestion, body text, I/O or execution is consulted.
        Accessor failures return unknown without exposing the object's repr.
    """
    if not isinstance(attributes, Mapping):
        return None
    try:
        if "gen_ai.operation.name" in attributes:
            value = attributes["gen_ai.operation.name"]
            return value if type(value) is str else None
        kind = attributes.get("openinference.span.kind")
        return _KINDS.get(kind) if type(kind) is str else None
    except Exception:
        return None


def semantic_key(key: Any, operation: str | None) -> str | None:
    """Project one supported source key into the canonical Trace vocabulary.

    Args:
        key: Observed attribute name; unsupported objects are not coerced.
        operation: Identity resolved from the same span by observed_operation.
    Returns:
        A canonical alias, or ``None`` to retain normal allowlist rejection.
        Tool bodies require an actual TOOL/execute_tool span; advertised tool
        schemas or LLM tool-call proposals do not establish execution.
    Postconditions:
        Values remain untouched for the existing bounded privacy normalizer.
        Generic input/output bodies never bypass identity-specific validation.
    Side Effects:
        None; this pure mapping neither imports instrumentation nor performs I/O.
    """
    if type(key) is not str or operation is None:
        return None
    if key == "openinference.span.kind":
        return "gen_ai.operation.name"
    if operation == "execute_tool":
        return _TOOL_FIELDS.get(key)
    return _METADATA.get(key)
