"""Bounded tool content normalization for real OTel execute-tool observations."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from ..repository.privacy import scan_sensitive_json
from ..repository.tool_capabilities import _plain_json
from .runtime_contract import RUNTIME_AGENT_OUTPUT_MAX_BYTES

TOOL_CONTENT_KEYS = {
    "gen_ai.tool.call.arguments": "arguments",
    "gen_ai.tool.call.result": "result",
}
TOOL_METADATA_KEYS = frozenset(
    {"gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.tool.call.id"}
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON object keys without exposing tool values."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate tool JSON key")
        result[key] = value
    return result


def normalize_tool_content(value: Any) -> tuple[Any, str]:
    """Detach one observed tool argument/result or return a safe omission status.

    Args:
        value: Actual semantic attribute from an ended ``execute_tool`` span;
            finite JSON, a JSON-encoded string, or an ordinary text result.

    Returns:
        Detached JSON and ``present``, or ``None`` with ``size_limit``,
        ``sensitive_content`` or ``invalid``. A present JSON null is distinct
        from an absent attribute. JSON strings are decoded at most once.

    Preconditions:
        The mapper has verified the span operation; this function does not
        infer execution or tool identity from arbitrary names or messages.

    Postconditions:
        Retained content is finite, at most depth 32 (root zero), and at most
        the existing 4 MiB Agent-output canonical JSON bound. Nothing is
        truncated. Callers own omission counters and partial capture status.

    Side Effects:
        No I/O or tool execution; only existing JSON and privacy validation.

    Security/Privacy:
        The canonical sensitive scanner always applies, without an
        ``allow_sensitive`` override. Unsupported objects are never repr'd.
    """
    try:
        if type(value) is str:
            if len(value.encode("utf-8")) > RUNTIME_AGENT_OUTPUT_MAX_BYTES:
                return None, "size_limit"
            with suppress(json.JSONDecodeError):
                value = json.loads(value, object_pairs_hook=_unique_object)
        plain = _plain_json(value)
        encoded = json.dumps(
            plain,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > RUNTIME_AGENT_OUTPUT_MAX_BYTES:
            return None, "size_limit"
        if scan_sensitive_json(plain, location="trace_tool_content"):
            return None, "sensitive_content"
        return plain, "present"
    except Exception:
        return None, "invalid"
