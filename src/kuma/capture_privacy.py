"""Known-PII projection for capture/trajectory review, not general Evidence policy."""

from __future__ import annotations

import json
import re
from typing import Any

from .repository.privacy import redact_sensitive_json

_PII = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|(?i:[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+|/home/[^/\s\"']+|/root\b)"
)


def redact_capture_json(value: Any) -> tuple[Any, bool]:
    """Detach bounded JSON and redact known review-ingress PII and credentials.

    Args:
        value: Finite, depth/size-validated plain JSON from capture or trajectory
            preparation. Callers must apply their normal graph budget first.
    Returns:
        Detached safe value and whether any content changed. Recognized email,
        IPv4 and user-home spellings match the review service's known-PII policy;
        credential handling remains owned by the existing canonical redactor.
    Preconditions:
        The caller has validated the graph; this helper is not an input validator.
    Postconditions:
        Originals are unchanged. Ordinary multilingual text and source references
        without recognized sensitive spellings are preserved. Detection is not a
        guarantee against unknown PII formats. Callers retain source fingerprints
        and record redaction before persistence, then recheck serialized budgets.
    Side Effects:
        None. No reads, network, Agent execution or general Case policy changes.
    """
    safe, credential_changed = redact_sensitive_json(value)
    projected = _pii(safe)
    return projected, credential_changed or projected != safe


def _pii(value: Any, depth: int = 0) -> Any:
    """Project validated JSON recursively, including JSON-encoded observed bodies.

    Keys are sanitized too; collisions receive deterministic non-secret labels
    instead of silently overwriting retained facts. Source objects are not mutated.
    """
    if depth > 32:
        return "[OMITTED: encoded content depth; evidence gap]"
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            safe_key = _pii(key, depth + 1)
            while safe_key in result:
                safe_key = "[REDACTED KEY " + str(len(result)) + "]" + safe_key
            result[safe_key] = _pii(child, depth + 1)
        return result
    if isinstance(value, list):
        return [_pii(child, depth + 1) for child in value]
    if not isinstance(value, str):
        return value
    if value.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except (ValueError, RecursionError):
            nested = None
        if isinstance(nested, dict | list):
            safe = _pii(nested, depth + 1)
            return value if safe == nested else json.dumps(safe, ensure_ascii=False)
    return _PII.sub("[REDACTED]", value)
