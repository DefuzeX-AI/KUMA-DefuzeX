"""High-confidence sensitive-data checks used immediately before upload."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._json_values import detach_json
from ..errors import SensitiveDataError

REDACTED = "[REDACTED]"
_CREDENTIAL_NAME = (
    r"(?:[A-Za-z][A-Za-z0-9_.-]*[_.-])?"
    r"(?:api[_-]?key|(?:access|auth|refresh|tampered|session)[_-]?token|token|"
    r"client[_-]secret|secret[_-]access[_-]key|private[_-]key|"
    r"credential(?:s)?|password|passwd|secret|auth(?:orization)?|(?:set[_-]?)?cookie)"
)
_CREDENTIAL_KEY = re.compile(rf"(?i)^{_CREDENTIAL_NAME}$")
_ASSIGNMENT = re.compile(
    rf"(?i)(?<![\w])(?P<label>{_CREDENTIAL_NAME}[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}]+)"
)
_HEADER_LINE = re.compile(r"(?im)\b(?:authorization|(?:set-)?cookie)\s*:[^\r\n]*")

_SENSITIVE_BASENAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".pem"})
_TEXT_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "authorization",
        re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S{8,}"),
    ),
    ("cookie", re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\s=;]+=[^\s;]{6,}")),
    ("authorization_bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "sk_api_key",
        re.compile(r"\bsk-(?:(?:proj|ant-api03)-)?[A-Za-z0-9_-]{12,}\b"),
    ),
    ("kuma_key", re.compile(r"\bdfx_[A-Za-z0-9_-]{6,}\.[A-Za-z0-9._-]{12,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.=-]{16,}"
        ),
    ),
)
PRIVATE_DATA_FIELDS = frozenset(
    {
        "answer_key",
        "deepseek_key",
        "expected_answer",
        "expected_output",
        "hidden_answer",
        "hidden_inputs",
        "internal_labels",
        "mcp_address",
        "mcp_url",
        "model_config",
        "private_prompt",
        "private_rubric",
        "provider_key",
        "system_prompt",
    }
)


@dataclass(frozen=True, slots=True)
class SensitiveFinding:
    """Identify a sensitive-data category without retaining the matched value.

    Attributes:
        kind: Stable category such as credential file, API token, or private field.
        location: Caller-supplied safe component/path label, not secret content.
    """

    kind: str
    location: str

    @property
    def reason(self) -> str:
        """Return the stable public reason code for this sensitive-data finding."""
        return f"sensitive_{self.kind}:{self.location}"


def scan_sensitive_path(
    path: str | Path, *, location: str = "file"
) -> tuple[SensitiveFinding, ...]:
    """Return a finding when a path suggests credentials or private data."""
    candidate = Path(path)
    basename = candidate.name.casefold()
    if (
        basename in _SENSITIVE_BASENAMES
        or basename.startswith(".env.")
        or candidate.suffix.casefold() in _SENSITIVE_SUFFIXES
    ):
        return (SensitiveFinding("credential_file", location),)
    return ()


def scan_sensitive_text(text: str, *, location: str) -> tuple[SensitiveFinding, ...]:
    """Scan bounded text for credential and private-data signatures."""
    findings = [
        SensitiveFinding(kind, location)
        for kind, pattern in _TEXT_PATTERNS
        if pattern.search(text) is not None
    ]
    if any(_assignment_is_secret(match) for match in _ASSIGNMENT.finditer(text)):
        findings.append(SensitiveFinding("credential_assignment", location))
    return tuple(findings)


def _assignment_is_secret(match: re.Match[str]) -> bool:
    """Exclude only the exact redaction marker or empty quoted assignment.

    This rule is shared by detection and outbound replacement; a marker prefix
    followed by other content is not an exemption. Token counts are not matched
    because their field names are outside the closed credential-name pattern.
    """
    value = match["value"].strip("\"'")
    return value not in {"", REDACTED}


def redact_sensitive_text(text: str) -> str:
    """Return a safe text copy, replacing recognized credentials explicitly.

    Args:
        text: Already bounded text owned by the Evidence projection caller.

    Returns:
        Unchanged safe text or text with literal ``[REDACTED]`` markers. Any
        private-key BEGIN marker omits the entire text, including incomplete
        PEM blocks; headers are removed as whole lines rather than leaving
        unsafe cookie tails. No claim of detecting unknown secrets is made.

    Postconditions:
        Reapplying this function is idempotent. Caller input is never mutated.
        No matched values are retained in metadata, exceptions, or logs.

    Side Effects:
        None. Callers must recompute digests and mark changed Evidence partial;
        this function must not be applied indiscriminately to protocol IDs.
    """
    if _TEXT_PATTERNS[0][1].search(text):
        return REDACTED
    for match in _ASSIGNMENT.finditer(text):
        scalar = match["value"]
        if scalar.startswith(("'", '"')) and (
            len(scalar) == 1 or scalar[-1] != scalar[0]
        ):
            return REDACTED
    result = _HEADER_LINE.sub(REDACTED, text)

    def replace_assignment(match: re.Match[str]) -> str:
        """Retain an assignment label but never its sensitive scalar value."""
        if not _assignment_is_secret(match):
            return match[0]
        return match["label"] + '"' + REDACTED + '"'

    result = _ASSIGNMENT.sub(replace_assignment, result)
    for kind, pattern in _TEXT_PATTERNS:
        if kind not in {
            "private_key",
            "authorization",
            "cookie",
            "credential_assignment",
        }:
            result = pattern.sub(REDACTED, result)
    return result


def redact_sensitive_json(value: Any) -> tuple[Any, bool]:
    """Detach and sanitize a payload graph without altering its original.

    Args:
        value: Finite, acyclic JSON content, not an envelope containing protocol
            IDs. The canonical JSON depth/type validation runs before traversal.

    Returns:
        Detached JSON plus whether any content was removed/replaced. Credential
        values use the literal marker; secret-bearing keys are omitted, never
        renamed to fabricated keys. Numeric token usage and ordinary words stay.
        JSON encoded inside a string is sanitized but remains a string.

    Raises:
        JsonStructureError: The canonical graph validator rejects invalid input.

    Postconditions:
        Safe inputs retain their content and types. Reapplying redaction does
        not change the result. No original values are exposed in diagnostics.

    Side Effects:
        No filesystem or network access. The caller owns byte limits, hashes,
        association checks and schema-specific loss accounting.
    """
    return _redact_graph(detach_json(value), 0)


def _credential_value(key: str, value: Any, *, containers: bool = False) -> bool:
    """Match credential values without mistaking JSON Schema declarations for data.

    Ordinary validation scans string values recursively, preserving previously
    supported schema property objects. Outbound Evidence redaction alone may
    conservatively replace whole credential-labelled containers via ``containers``.
    Numeric usage and exact markers are never treated as credential payloads.
    """
    normalized = "_".join(
        part for part in re.split(r"[^a-z0-9]+", key.casefold()) if part
    )
    return bool(_CREDENTIAL_KEY.fullmatch(normalized)) and (
        (containers and isinstance(value, (dict, list)))
        or (isinstance(value, str) and value not in {"", REDACTED})
    )


def _redact_mapping(value: dict[str, Any], depth: int) -> tuple[dict[str, Any], bool]:
    """Copy JSON fields, dropping secret keys rather than inventing replacements."""
    result = {}
    changed = False
    for key, child in value.items():
        if redact_sensitive_text(key) != key:
            changed = True
            continue
        if _credential_value(key, child, containers=True):
            result[key] = REDACTED
            changed = True
        else:
            result[key], child_changed = _redact_graph(child, depth)
            changed |= child_changed
    return result, changed


def _redact_string(value: str, depth: int) -> tuple[str, bool]:
    """Preserve string type while inspecting at most eight nested JSON encodings.

    The outer graph is already validated. Failed decoding falls back to text
    rules; exceeding the decoder bound omits the complete scalar. Unchanged
    encoded strings preserve original whitespace and ordering exactly.
    """
    if value.lstrip().startswith(("{", "[", '"')) and value != REDACTED:
        try:
            decoded = detach_json(json.loads(value, object_pairs_hook=_unique_fields))
        except (ValueError, TypeError, RecursionError):
            pass
        else:
            if depth >= 8:
                return REDACTED, True
            safe, changed = _redact_graph(decoded, depth + 1)
            if changed:
                return json.dumps(safe, ensure_ascii=True, separators=(",", ":")), True
            return value, False
    safe = redact_sensitive_text(value)
    return safe, safe != value


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys so decoding cannot hide a sensitive occurrence."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous JSON object")
        result[key] = value
    return result


def _redact_graph(value: Any, depth: int) -> tuple[Any, bool]:
    """Traverse a detached graph; depth counts encoded strings, not containers."""
    if isinstance(value, dict):
        return _redact_mapping(value, depth)
    if isinstance(value, list):
        children = [_redact_graph(child, depth) for child in value]
        return [safe for safe, _ in children], any(changed for _, changed in children)
    if isinstance(value, str):
        return _redact_string(value, depth)
    return value, False


def scan_sensitive_json(value: Any, *, location: str) -> tuple[SensitiveFinding, ...]:
    """Recursively scan JSON keys and scalar values for sensitive data."""
    findings: list[SensitiveFinding] = []

    def visit(item: Any) -> None:
        """Visit nested JSON values while applying the sensitive-data scanner."""
        if isinstance(item, Mapping):
            for key, child in item.items():
                if _credential_value(str(key), child):
                    findings.append(SensitiveFinding("credential_field", location))
                findings.extend(scan_sensitive_text(str(key), location=location))
                visit(child)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            findings.extend(scan_sensitive_text(item, location=location))

    visit(value)
    return tuple(findings)


def contains_private_data(value: Any, *, extra_fields: Sequence[str] = ()) -> bool:
    """Detect protocol fields that must not cross a public SDK response."""

    prohibited = PRIVATE_DATA_FIELDS | {field.casefold() for field in extra_fields}

    def visit(item: Any) -> bool:
        """Visit nested JSON values while applying the sensitive-data scanner."""
        if isinstance(item, Mapping):
            if {str(key).casefold() for key in item} & prohibited:
                return True
            return any(visit(child) for child in item.values())
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return any(visit(child) for child in item)
        return False

    return visit(value)


def enforce_sensitive_policy(
    findings: Sequence[SensitiveFinding],
    *,
    allow_sensitive: bool,
) -> None:
    """Raise before transport when findings are not explicitly allowed."""
    if not findings or allow_sensitive:
        return
    reasons = tuple(dict.fromkeys(item.reason for item in findings))
    raise SensitiveDataError(
        "Sensitive data was detected; remove it or explicitly set allow_sensitive=True",
        details={"reasons": reasons},
    )


__all__ = [
    "SensitiveFinding",
    "contains_private_data",
    "enforce_sensitive_policy",
    "redact_sensitive_json",
    "redact_sensitive_text",
    "scan_sensitive_json",
    "scan_sensitive_path",
    "scan_sensitive_text",
]
