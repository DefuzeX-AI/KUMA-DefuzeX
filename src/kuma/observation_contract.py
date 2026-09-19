"""Closed cloud observation admission using the existing canonical Trace validators."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from ._json_values import detach_json
from .errors import ProviderError, ValidationError
from .evidence.observation_capture import MAX_OBSERVATION_BYTES
from .evidence.runtime_trace_contract import _require, _span, _validate_capture
from .evidence.trace import _CAPTURE_REASONS
from .repository.privacy import scan_sensitive_json

_EXPORT_FIELDS = {
    "schema_version",
    "observation_id",
    "external_run_id",
    "external_invocation_id",
    "execution_status",
    "evaluation_status",
    "duration_ms",
    "sdk_version",
    "spans",
    "capture_status",
    "capture_summary",
    "reasons",
    "dropped_count",
    "truncated",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "observation_id",
    "content_sha256",
    "upload_status",
    "evaluation_status",
    "created_at",
    "cleanup_eligible_at",
    "stored_bytes",
}


def canonical_bytes(value: Any) -> bytes:
    """Encode admitted finite JSON exactly as the cloud content hash contract."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def checked(function: Callable[[Any], Any], value: Any, *, remote: bool = False) -> Any:
    """Detach untrusted JSON and sanitize all validation failures outside handlers.

    Local invalid input fails before HTTP. Remote invalid bodies never escape as
    values or exception chains; callers still receive the stable error category.
    Does not catch process-control exceptions, log content or perform I/O.
    """
    failed = False
    try:
        plain = detach_json(value)
        _require(not scan_sensitive_json(plain, location="observation"))
        result = function(plain)
    except Exception:
        failed = True
    if failed:
        if remote:
            raise ProviderError(
                "The service returned an invalid observation response",
                code="invalid_response",
            )
        raise ValidationError(
            "Observation input is invalid, sensitive or exceeds its limits",
            code="observation_invalid",
        )
    return result


def identifier(value: Any) -> str:
    """Accept one path-safe opaque observation ID, never caller URLs or paths."""
    _require(
        type(value) is str and re.fullmatch(r"obs_[0-9a-f]{32}", value) is not None
    )
    return value


def page_limit(value: Any) -> int:
    """Reject bool/float coercion and cap each explicit metadata read at 100 items."""
    _require(type(value) is int and 1 <= value <= 100)
    return value


def closed(value: Any, fields: set[str]) -> dict[str, Any]:
    """Require exactly one public object shape without silently dropping fields."""
    _require(isinstance(value, dict) and set(value) == fields)
    return value


def metadata(value: Mapping[str, Any]) -> None:
    """Validate execution/capture metadata without interpreting it as evaluation."""
    _require(value["execution_status"] in {"completed", "failed", "aborted", "unknown"})
    _require(value["capture_status"] in {"complete", "partial", "failed"})
    duration = value["duration_ms"]
    _require(
        duration is None or (type(duration) is int and 0 <= duration <= 86_400_000)
    )
    _require(type(value["sdk_version"]) is str and 1 <= len(value["sdk_version"]) <= 32)


def export(value: Any) -> dict[str, Any]:
    """Validate a detached local export for explicit upload or private detail.

    Reuses ended-span/capture/topology/model/tool rules without inventing Case,
    Run or Judge identities. Five MiB bounds the complete canonical export;
    oversize and malformed exports are rejected, never truncated or rewritten.
    The enclosing checked boundary applies the canonical credential scanner.
    """
    value = closed(value, _EXPORT_FIELDS)
    _require(value["schema_version"] == "kuma.observation.v1")
    _require(value["evaluation_status"] == "not_performed")
    identifier(value["observation_id"])
    metadata(value)
    for field in ("external_run_id", "external_invocation_id"):
        label = value[field]
        _require(
            label is None
            or (
                type(label) is str
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", label)
                is not None
            )
        )
    _require(len(canonical_bytes(value)) <= MAX_OBSERVATION_BYTES)
    _require(type(value["truncated"]) is bool)
    _require(type(value["dropped_count"]) is int and value["dropped_count"] >= 0)
    reasons, spans = value["reasons"], value["spans"]
    _require(isinstance(reasons, list) and len(reasons) <= 64)
    _require(
        all(type(reason) is str and reason in _CAPTURE_REASONS for reason in reasons)
    )
    _require(len(set(reasons)) == len(reasons))
    _require(isinstance(spans, list) and len(spans) <= 10_000)
    identities = set()
    for span in spans:
        _span(span)
        identity = (span["trace_id"], span["span_id"])
        _require(identity not in identities)
        identities.add(identity)
    _validate_capture(value, value, identities)
    return value


def receipt(value: Any, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate closed receipt and, when known, exact export ID/hash/byte count."""
    value = closed(value, _RECEIPT_FIELDS)
    _require(value["schema_version"] == "kuma.observation_receipt.v1")
    identifier(value["observation_id"])
    _require(
        value["upload_status"] == "accepted"
        and value["evaluation_status"] == "not_performed"
    )
    _require(
        type(value["content_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", value["content_sha256"]) is not None
    )
    _require(
        type(value["stored_bytes"]) is int
        and 1 <= value["stored_bytes"] <= MAX_OBSERVATION_BYTES
    )
    dates = []
    for field in ("created_at", "cleanup_eligible_at"):
        raw = value[field]
        _require(type(raw) is str and len(raw) <= 40)
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        _require(date.utcoffset() == timedelta(0))
        dates.append(date)
    _require(dates[1] >= dates[0])
    if body is not None:
        encoded = canonical_bytes(body)
        _require(value["observation_id"] == body["observation_id"])
        _require(value["stored_bytes"] == len(encoded))
        _require(value["content_sha256"] == hashlib.sha256(encoded).hexdigest())
    return value


def detail(value: Any, observation_id: str) -> dict[str, Any]:
    """Bind private detail to its requested ID and validated receipt/export hash."""
    value = closed(value, {"schema_version", "receipt", "observation"})
    _require(value["schema_version"] == "kuma.observation_detail.v1")
    body = export(value["observation"])
    _require(receipt(value["receipt"], body=body)["observation_id"] == observation_id)
    return value


def page(value: Any, limit: int) -> dict[str, Any]:
    """Bound metadata-only pagination and require its last ID as continuation."""
    value = closed(value, {"schema_version", "items", "next_cursor"})
    _require(value["schema_version"] == "kuma.observation_list.v1")
    _require(isinstance(value["items"], list) and len(value["items"]) <= limit)
    seen = set()
    for item in value["items"]:
        closed(
            item,
            {
                "receipt",
                "execution_status",
                "capture_status",
                "duration_ms",
                "sdk_version",
            },
        )
        metadata(item)
        item_id = receipt(item["receipt"])["observation_id"]
        _require(item_id not in seen)
        seen.add(item_id)
    if value["next_cursor"] is not None:
        identifier(value["next_cursor"])
        _require(
            bool(value["items"])
            and value["next_cursor"] == value["items"][-1]["receipt"]["observation_id"]
        )
    return value


def deleted(value: Any, observation_id: str) -> dict[str, Any]:
    """Validate idempotent deletion acknowledgment without claiming prior existence."""
    value = closed(value, {"schema_version", "observation_id", "deleted"})
    _require(value["schema_version"] == "kuma.observation_deleted.v1")
    _require(value["observation_id"] == observation_id and value["deleted"] is True)
    return value
