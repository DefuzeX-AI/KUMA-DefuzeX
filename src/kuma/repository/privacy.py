"""High-confidence sensitive-data checks used immediately before upload."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SensitiveDataError

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
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "authorization",
        re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S{8,}"),
    ),
    ("cookie", re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*[^\s=;]+=[^\s;]{6,}")),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("kuma_key", re.compile(r"\bdfx_[A-Za-z0-9_-]{6,}\.[A-Za-z0-9._-]{12,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9+/_.=-]{16,}"
        ),
    ),
)
_SENSITIVE_FIELD = re.compile(
    r"(?i)^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|password|secret)$"
)
PRIVATE_DATA_FIELDS = frozenset(
    {
        "answer_key",
        "deepseek_key",
        "expected_answer",
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
    kind: str
    location: str

    @property
    def reason(self) -> str:
        return f"sensitive_{self.kind}:{self.location}"


def scan_sensitive_path(
    path: str | Path, *, location: str = "file"
) -> tuple[SensitiveFinding, ...]:
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
    findings = [
        SensitiveFinding(kind, location)
        for kind, pattern in _TEXT_PATTERNS
        if pattern.search(text) is not None
    ]
    return tuple(findings)


def scan_sensitive_json(value: Any, *, location: str) -> tuple[SensitiveFinding, ...]:
    findings: list[SensitiveFinding] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if (
                    _SENSITIVE_FIELD.fullmatch(str(key)) is not None
                    and isinstance(child, str)
                    and len(child.strip()) >= 12
                ):
                    findings.append(SensitiveFinding("credential_field", location))
                visit(child)
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for child in item:
                visit(child)

    visit(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return tuple(findings)
    findings.extend(scan_sensitive_text(serialized, location=location))
    return tuple(findings)


def contains_private_data(value: Any, *, extra_fields: Sequence[str] = ()) -> bool:
    """Detect protocol fields that must not cross a public SDK response."""

    prohibited = PRIVATE_DATA_FIELDS | {field.casefold() for field in extra_fields}

    def visit(item: Any) -> bool:
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
    "scan_sensitive_json",
    "scan_sensitive_path",
    "scan_sensitive_text",
]
