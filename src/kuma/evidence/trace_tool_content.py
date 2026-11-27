"""Bounded tool content normalization for real OTel execute-tool observations."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from ..repository.privacy import redact_sensitive_json, scan_sensitive_json
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


def normalize_tool_content(
    value: Any, *, redact: bool = False, _redact_sensitive: bool = True
) -> tuple[Any, str]:
    """Detach one observed tool argument/result or return a safe omission status.

    Args:
        value: Actual semantic attribute from an ended ``execute_tool`` span;
            finite JSON, a JSON-encoded string, or an ordinary text result.
        redact: Apply the shared Evidence sanitizer before the final scan.
            False preserves historical whole-field omission; True can return
            a detached partial value with ``redacted`` status. Callers must
            negotiate that status before upload. Both remain protected by default.
        _redact_sensitive: Internal strict Boolean, default True. Only an explicitly
            warned local capture/trajectory operation passes False to retain bounded
            originals. Classic Run and cloud Observation callers keep the default.

    Returns:
        Detached JSON and ``present``/``redacted``, or ``None`` with ``size_limit``,
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
        The canonical sensitive scanner applies under the default privacy policy;
        legacy ``allow_sensitive`` does not override it. Unsupported objects are
        never repr'd. Raw policy does not weaken graph, size or identity validation.
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
        changed = False
        if type(_redact_sensitive) is not bool:
            return None, "invalid"
        if redact and _redact_sensitive:
            plain, changed = redact_sensitive_json(plain)
            if (
                len(
                    json.dumps(
                        plain,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
                > RUNTIME_AGENT_OUTPUT_MAX_BYTES
            ):
                return None, "size_limit"
        if _redact_sensitive and scan_sensitive_json(
            plain, location="trace_tool_content"
        ):
            return None, "sensitive_content"
        return plain, "redacted" if changed else "present"
    except Exception:
        return None, "invalid"
