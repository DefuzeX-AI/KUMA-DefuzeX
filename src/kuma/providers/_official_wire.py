"""Shared validation for data crossing the official Provider boundary."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from ..errors import ProviderError
from ..evidence.trace_mapping import attribute_key_allowed
from ..repository.privacy import contains_private_data, scan_sensitive_json
from .base import JudgeContext

_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_OFFICIAL_CASE_PROVENANCE_FIELDS = (
    "batch_id",
    "case_sha256",
    "case_signature",
    "repo_fingerprint",
    "schema_version",
    "strategy_id",
    "strategy_version",
)
_TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "case_id",
        "input_id",
        "spans",
        "dropped_count",
        "truncated",
        "reasons",
    }
)
_SPAN_FIELDS = frozenset(
    {
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "kind",
        "status",
        "start_time_unix_nano",
        "end_time_unix_nano",
        "duration_nano",
        "attributes",
        "events",
        "resource",
        "scope",
    }
)


def plain_json(value: Any) -> Any:
    """Return a detached JSON value after rejecting unsupported objects."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain_json(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(child) for child in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def canonical_sha256(value: Any) -> str:
    """Return a deterministic SHA-256 digest of a JSON-compatible value."""
    raw = json.dumps(
        plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def contains_private_fields(value: Any) -> bool:
    """Return whether private fields satisfies the official public-wire validation contract."""
    return contains_private_data(value, extra_fields=("rubric",))


def required_text(value: Any, label: str) -> str:
    """Require a non-empty public text field from an official response."""
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(
            f"The Backend returned an invalid {label}", code="invalid_response"
        )
    return value


def validate_official_case_provenance(value: Any) -> dict[str, str]:
    """Validate SDK-owned metadata that distinguishes an official Case."""

    if not isinstance(value, Mapping) or contains_private_fields(value):
        raise ProviderError(
            "Official Case provenance is invalid", code="invalid_response"
        )
    provenance = {
        name: required_text(value.get(name), f"Official Case {name}")
        for name in _OFFICIAL_CASE_PROVENANCE_FIELDS
    }
    case_sha256 = provenance["case_sha256"]
    repo_fingerprint = provenance["repo_fingerprint"].removeprefix("sha256:")
    if (
        _SHA256_DIGEST.fullmatch(case_sha256) is None
        or _SHA256_DIGEST.fullmatch(repo_fingerprint) is None
        or provenance["strategy_id"] == "auto"
    ):
        raise ProviderError(
            "Official Case provenance is invalid", code="invalid_response"
        )
    return provenance


def valid_judgment_issue(value: Mapping[str, Any]) -> bool:
    """Return whether judgment issue satisfies the official public-wire validation contract."""
    return (
        isinstance(value.get("issue_id"), str)
        and bool(value["issue_id"])
        and value.get("severity") in {"low", "medium", "high"}
        and isinstance(value.get("message"), str)
        and bool(value["message"])
    )


def valid_step_result(value: Mapping[str, Any]) -> bool:
    """Return whether step result satisfies the official public-wire validation contract."""
    return (
        isinstance(value.get("step_id"), str)
        and bool(value["step_id"])
        and value.get("verdict") in {"pass", "passed", "issue", "insufficient_evidence"}
        and isinstance(value.get("issues", []), list)
        and all(isinstance(item, str) for item in value.get("issues", []))
    )


def _valid_trace_attributes(value: Any, *, resource: bool = False) -> bool:
    """Return whether trace attributes satisfies the official public-wire validation contract."""
    return isinstance(value, Mapping) and all(
        isinstance(key, str) and attribute_key_allowed(key, resource=resource)
        for key in value
    )


def _valid_trace_event(value: Any) -> bool:
    """Return whether trace event satisfies the official public-wire validation contract."""
    return (
        isinstance(value, Mapping)
        and set(value) == {"name", "time_unix_nano", "attributes"}
        and isinstance(value["name"], str)
        and _valid_non_negative_int(value["time_unix_nano"])
        and _valid_trace_attributes(value["attributes"])
    )


def _valid_non_negative_int(value: Any) -> bool:
    """Return whether non negative int satisfies the official public-wire validation contract."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_trace_span(value: Any) -> bool:
    """Return whether trace span satisfies the official public-wire validation contract."""
    if not isinstance(value, Mapping) or set(value) != _SPAN_FIELDS:
        return False
    parent = value["parent_span_id"]
    start = value["start_time_unix_nano"]
    end = value["end_time_unix_nano"]
    duration = value["duration_nano"]
    events = value["events"]
    scope = value["scope"]
    return (
        isinstance(value["trace_id"], str)
        and _TRACE_ID.fullmatch(value["trace_id"]) is not None
        and isinstance(value["span_id"], str)
        and _SPAN_ID.fullmatch(value["span_id"]) is not None
        and (parent is None or (isinstance(parent, str) and _SPAN_ID.fullmatch(parent)))
        and isinstance(value["name"], str)
        and value["kind"]
        in {"unspecified", "internal", "server", "client", "producer", "consumer"}
        and value["status"] in {"unset", "ok", "error"}
        and all(_valid_non_negative_int(item) for item in (start, end, duration))
        and end >= start
        and duration == end - start
        and _valid_trace_attributes(value["attributes"])
        and isinstance(events, Sequence)
        and not isinstance(events, str | bytes | bytearray)
        and all(_valid_trace_event(event) for event in events)
        and _valid_trace_attributes(value["resource"], resource=True)
        and isinstance(scope, Mapping)
        and set(scope) == {"name", "version"}
        and all(isinstance(scope[key], str) for key in ("name", "version"))
    )


def valid_trace_evidence(
    value: Any, *, run_id: str, case_id: str, input_id: str
) -> bool:
    """Return whether trace evidence satisfies the official public-wire validation contract."""
    if not isinstance(value, Mapping) or set(value) != _TRACE_FIELDS:
        return False
    spans = value["spans"]
    reasons = value["reasons"]
    return (
        value["schema_version"] == "defuzex.trace_evidence.v1"
        and (value["run_id"], value["case_id"], value["input_id"])
        == (run_id, case_id, input_id)
        and isinstance(spans, Sequence)
        and not isinstance(spans, str | bytes | bytearray)
        and all(_valid_trace_span(span) for span in spans)
        and _valid_non_negative_int(value["dropped_count"])
        and isinstance(value["truncated"], bool)
        and isinstance(reasons, Sequence)
        and not isinstance(reasons, str | bytes | bytearray)
        and all(isinstance(reason, str) for reason in reasons)
        and not contains_private_fields(value)
        and not scan_sensitive_json(value, location="trace_evidence")
    )


def history_evidence(context: JudgeContext) -> list[dict[str, Any]]:
    """Build the allowlisted public history payload for Official Judge."""

    history: list[dict[str, Any]] = []
    for item in context.history:
        submission: dict[str, Any] = {
            "status": item.submission.status,
            "output": plain_json(item.submission.output),
            "error": item.submission.error,
            "capture_status": plain_json(item.submission.capture_status),
            "logs": plain_json(item.submission.logs),
            "file_evidence": plain_json(item.submission.file_evidence),
            "dropped_count": item.submission.dropped_count,
            "missing": list(item.submission.missing),
        }
        if "trace_evidence" in item.submission.extensions:
            trace_evidence = item.submission.extensions["trace_evidence"]
            if not valid_trace_evidence(
                trace_evidence,
                run_id=item.submission.run_id,
                case_id=item.submission.case_id,
                input_id=item.submission.input_id,
            ):
                raise ProviderError(
                    "Trace Evidence is invalid", code="trace_evidence_invalid"
                )
            submission["trace_evidence"] = plain_json(trace_evidence)
        history.append(
            {
                "input": {
                    "input_id": item.test_input.input_id,
                    "index": item.test_input.index,
                },
                "submission": submission,
            }
        )
    return history


__all__ = [
    "canonical_sha256",
    "contains_private_fields",
    "history_evidence",
    "plain_json",
    "required_text",
    "valid_judgment_issue",
    "valid_step_result",
    "valid_trace_evidence",
    "validate_official_case_provenance",
]
