"""Versioned, caller-owned requirements and tool policies for process evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError
from .evidence.trace_tool_content import normalize_tool_content
from .trajectory_privacy import LOCAL_WARNING, privacy_operation

VERSION = "kuma.trajectory_requirements.v1"
_FIELDS = {
    "schema_version",
    "name",
    "version",
    "task_type",
    "objective",
    "success_criteria",
    "constraints",
    "tools",
    "retry_policy",
    "budgets",
    "reference",
    "limitations",
}


def requirements_template() -> dict[str, Any]:
    """Return an editable requirements document with explicit unknowns, never invented answers.

    Returns:
        Detached v1 dictionary. Fill name, version, objective and criteria before validation;
        null tools means unavailable catalog, whereas an empty list declares no tools.
    Side Effects:
        None; does not infer requirements, inspect an Agent or contact a service.
    """
    return {
        "schema_version": VERSION,
        "name": "",
        "version": "",
        "task_type": "",
        "objective": "",
        "success_criteria": [],
        "constraints": [],
        "tools": None,
        "retry_policy": {"normal_conditions": [], "stop_conditions": []},
        "budgets": {"duration_ms": None, "cost_usd": None, "tool_calls": None},
        "reference": None,
        "limitations": [],
    }


def _text(value: Any, size: int, *, empty: bool = False) -> None:
    """Validate bounded authored text without echoing its content in errors."""
    if (
        not isinstance(value, str)
        or len(value) > size
        or (not empty and not value.strip())
    ):
        raise ValidationError("Requirements contain invalid text")


def _object(value: Any, fields: set[str]) -> None:
    """Require a closed object so ignored policy fields cannot appear effective."""
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValidationError("Requirements contain unsupported or missing fields")


def _texts(value: Any, count: int = 32) -> None:
    """Validate a finite list of separate requirements or known evidence gaps."""
    if not isinstance(value, list) or len(value) > count:
        raise ValidationError("Requirements contain an invalid text list")
    for item in value:
        _text(item, 1500)


def _criteria(items: Any) -> None:
    """Require distinct user-owned check coordinates within the existing Judge check budget."""
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValidationError("Requirements need 1..16 success criteria")
    identities = set()
    for item in items:
        _object(item, {"id", "description", "priority", "evidence_expected"})
        _text(item["id"], 64)
        _text(item["description"], 1500)
        _texts(item["evidence_expected"], 8)
        if (
            not isinstance(item["priority"], str)
            or item["priority"] not in {"required", "preferred"}
            or item["id"] in identities
        ):
            raise ValidationError("Requirements have invalid or duplicate criteria")
        identities.add(item["id"])


def _tools(items: Any) -> None:
    """Check declarations of available tools and when to use them, never execute schemas."""
    if items is None:
        return
    if not isinstance(items, list) or len(items) > 256:
        raise ValidationError("Requirements contain an invalid tool catalog")
    names = set()
    for item in items:
        _object(
            item,
            {
                "name",
                "description",
                "input_schema",
                "output_schema",
                "use_when",
                "avoid_when",
                "prerequisites",
                "effects",
                "availability",
                "expected_errors",
            },
        )
        _text(item["name"], 200)
        _text(item["description"], 1500)
        if item["name"] in names:
            raise ValidationError("Requirements contain duplicate tools")
        names.add(item["name"])
        for key in ("input_schema", "output_schema"):
            if item[key] is not None and not isinstance(item[key], dict):
                raise ValidationError("Tool schemas must be objects or unknown")
        for key in (
            "use_when",
            "avoid_when",
            "prerequisites",
            "effects",
            "expected_errors",
        ):
            _texts(item[key], 16)
        _text(item["availability"], 1500)


@privacy_operation(LOCAL_WARNING)
def validate_requirements(
    value: Mapping[str, Any], *, redact_sensitive: bool = True
) -> dict[str, Any]:
    """Validate and detach a reviewed requirements document without changing caller policies.

    Args:
        value: Exact v1 template shape. Required nonblank name/version/objective and 1..16
            success criteria; task_type may be empty (unclassified); unknown tools/reference
            and each unknown budget are null. See docs/trajectory-platform.md for every field.
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    Returns:
        Detached finite JSON, at most 256 KiB and depth 32, preserving user-declared policies.
    Raises:
        ValidationError: Invalid/oversized shape or sensitive content requiring redaction.
            Requirements with redacted policies are rejected for user correction, not changed.
    Side Effects:
        Emits RawContentWarning when redact_sensitive=False; no model, network,
        files, credentials, tool/schema execution or scoring.
    """
    from .trajectory_privacy import checked_policy

    checked_policy(redact_sensitive)
    safe, status = normalize_tool_content(
        value, redact=True, _redact_sensitive=redact_sensitive
    )
    if status != "present":
        raise ValidationError("Requirements must be corrected before use")
    _object(safe, _FIELDS)
    if safe["schema_version"] != VERSION:
        raise ValidationError("Requirements schema is unsupported")
    for key, limit in (("name", 200), ("version", 80), ("objective", 12000)):
        _text(safe[key], limit)
    _text(safe["task_type"], 120, empty=True)
    _criteria(safe["success_criteria"])
    _tools(safe["tools"])
    _texts(safe["constraints"])
    _texts(safe["limitations"])
    _object(safe["retry_policy"], {"normal_conditions", "stop_conditions"})
    for conditions in safe["retry_policy"].values():
        _texts(conditions)
    _object(safe["budgets"], {"duration_ms", "cost_usd", "tool_calls"})
    for name, amount in safe["budgets"].items():
        if amount is None:
            continue
        types = (int, float) if name == "cost_usd" else (int,)
        if type(amount) not in types or not 0 <= amount <= 10**12:
            raise ValidationError("Requirements contain an invalid budget")
    if safe["reference"] is not None:
        _text(safe["reference"], 12000)
    if len(_bytes(safe)) > 262144:
        raise ValidationError("Requirements exceed 256 KiB")
    return safe


def _bytes(value: dict) -> bytes:
    """Encode a finite requirements graph with stable cross-process fingerprint semantics."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@privacy_operation(LOCAL_WARNING)
def requirements_fingerprint(
    value: Mapping[str, Any], *, redact_sensitive: bool = True
) -> str:
    """Return SHA-256 of canonical validated requirements; edits produce a different revision.

    Args:
        value: Reviewed requirements accepted by validate_requirements.
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    Returns:
        Lowercase 64-character content identity, not proof of truth or authority.
    Raises:
        ValidationError: The same invalid/sensitive/size conditions as validation.
    Side Effects:
        Emits RawContentWarning when redact_sensitive=False; no file I/O or network,
        and does not register or persist a revision.
    """
    return hashlib.sha256(
        _bytes(validate_requirements(value, redact_sensitive=redact_sensitive))
    ).hexdigest()
