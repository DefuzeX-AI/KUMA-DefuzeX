"""Deterministically fit Run Evidence to the public transport byte budget."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial
from typing import Any

from ..errors import LimitExceededError

_LOG_GAP = "log_content_transport_budget"
_TRACE_GAP = "trace_transport_budget"
_MAX_TRACE_DROPPED_COUNT = 999_999_999


def _encode(evidence: Mapping[str, Any]) -> bytes:
    """Serialize a value with the canonical compact UTF-8 JSON encoding."""
    return json.dumps(
        evidence,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fits(evidence: Mapping[str, Any], limit: int) -> bool:
    """Return whether canonical serialization fits the negotiated byte limit."""
    return len(_encode(evidence)) <= limit


def _submissions(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return mutable Submission projections from ordered history entries."""
    return [item["submission"] for item in evidence["history"]]


def _append_once(values: list[Any], reason: str) -> None:
    """Append one stable degradation reason without duplicates."""
    if reason not in values:
        values.append(reason)


def _mark_component(submission: dict[str, Any], name: str, reason: str) -> None:
    """Mark a non-failed capture component partial for transport truncation."""
    component = submission["capture_status"][name]
    if component.get("status") != "failed":
        component["status"] = "partial"
    _append_once(component["reasons"], reason)


def _mark_submission_gap(
    submission: dict[str, Any], *, component: str, reason: str, dropped: int
) -> None:
    """Record one Submission-level omission and its dropped item count."""
    _append_once(submission["missing"], reason)
    submission["dropped_count"] += dropped
    _mark_component(submission, component, reason)


def _log_entries(
    evidence: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ordered log entries paired with their owning Submission."""
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for submission in _submissions(evidence):
        result.extend((submission, log) for log in submission["logs"])
    return result


def _retain_log_prefix(
    *,
    log: dict[str, Any],
    submission: dict[str, Any],
    projection: dict[str, Any],
    content: str,
    characters: int,
    original_complete: Any,
    original_bytes: int,
    base_submission_drop: int,
    base_projection_drop: int,
) -> None:
    """Replace log content with a UTF-8 prefix and account for dropped bytes."""
    prefix = content[:characters]
    retained_bytes = len(prefix.encode("utf-8"))
    log.update(
        {
            "content": prefix,
            "complete": False,
            "truncated": True,
            "source_complete": original_complete,
            "original_content_utf8_bytes": original_bytes,
            "included_content_utf8_bytes": retained_bytes,
        }
    )
    submission["dropped_count"] = base_submission_drop + 1
    projection["dropped_log_content_utf8_bytes"] = (
        base_projection_drop + original_bytes - retained_bytes
    )


def _truncate_log_content(
    evidence: dict[str, Any], limit: int, projection: dict[str, Any]
) -> None:
    """Binary-search deterministic log prefixes until the upload fits."""
    for submission, log in reversed(_log_entries(evidence)):
        if _fits(evidence, limit):
            return
        content = log.get("content")
        if not isinstance(content, str) or not content:
            continue
        original_bytes = len(content.encode("utf-8"))
        original_complete = log.get("complete")
        base_projection_drop = projection["dropped_log_content_utf8_bytes"]
        base_submission_drop = submission.get("dropped_count", 0)
        _mark_submission_gap(
            submission,
            component="logs",
            reason=_LOG_GAP,
            dropped=1,
        )

        retain = partial(
            _retain_log_prefix,
            log=log,
            submission=submission,
            projection=projection,
            content=content,
            original_complete=original_complete,
            original_bytes=original_bytes,
            base_submission_drop=base_submission_drop,
            base_projection_drop=base_projection_drop,
        )
        retain(characters=0)
        if not _fits(evidence, limit):
            continue
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            retain(characters=middle)
            if _fits(evidence, limit):
                low = middle
            else:
                high = middle - 1
        retain(characters=low)


def _trace_entries(
    evidence: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ordered Trace envelopes paired with their owning Submission."""
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for submission in _submissions(evidence):
        trace = submission.get("trace_evidence")
        if isinstance(trace, dict) and isinstance(trace.get("spans"), list):
            result.append((submission, trace))
    return result


def _retain_trace_prefix(
    *,
    trace: dict[str, Any],
    submission: dict[str, Any],
    projection: dict[str, Any],
    original_spans: list[Any],
    count: int,
    original_trace_dropped: int,
    reasons: list[Any],
    base_submission_drop: int,
    base_projection_drop: int,
) -> None:
    """Retain an ordered span prefix and account for all removed spans."""
    dropped = len(original_spans) - count
    trace["spans"] = original_spans[:count]
    trace["dropped_count"] = min(
        original_trace_dropped + dropped,
        _MAX_TRACE_DROPPED_COUNT,
    )
    if original_trace_dropped + dropped > _MAX_TRACE_DROPPED_COUNT:
        _append_once(reasons, "trace_drop_count_saturated")
    submission["dropped_count"] = base_submission_drop + dropped
    projection["dropped_trace_spans"] = base_projection_drop + dropped


def _truncate_trace_spans(
    evidence: dict[str, Any], limit: int, projection: dict[str, Any]
) -> None:
    """Binary-search deterministic span prefixes until the upload fits."""
    for submission, trace in reversed(_trace_entries(evidence)):
        if _fits(evidence, limit):
            return
        spans = trace["spans"]
        if not spans:
            continue
        original_spans = list(spans)
        original_trace_dropped = trace.get("dropped_count", 0)
        base_projection_drop = projection["dropped_trace_spans"]
        base_submission_drop = submission.get("dropped_count", 0)
        reasons = trace["reasons"]
        _append_once(reasons, _TRACE_GAP)
        trace["truncated"] = True
        _mark_submission_gap(
            submission,
            component="traces",
            reason=f"trace_evidence:{_TRACE_GAP}",
            dropped=len(original_spans),
        )

        retain = partial(
            _retain_trace_prefix,
            trace=trace,
            submission=submission,
            projection=projection,
            original_spans=original_spans,
            original_trace_dropped=original_trace_dropped,
            reasons=reasons,
            base_submission_drop=base_submission_drop,
            base_projection_drop=base_projection_drop,
        )
        retain(count=0)
        if not _fits(evidence, limit):
            continue
        low, high = 0, len(original_spans)
        while low < high:
            middle = (low + high + 1) // 2
            retain(count=middle)
            if _fits(evidence, limit):
                low = middle
            else:
                high = middle - 1
        retain(count=low)


def project_run_evidence(
    evidence: dict[str, Any], *, max_utf8_bytes: int
) -> tuple[dict[str, Any], bytes]:
    """Return a bounded transport projection without touching local Submissions."""

    original = _encode(evidence)
    if len(original) <= max_utf8_bytes:
        return evidence, original
    projection = {
        "complete": False,
        "limit_utf8_bytes": max_utf8_bytes,
        "original_utf8_bytes": len(original),
        "dropped_log_content_utf8_bytes": 0,
        "dropped_trace_spans": 0,
    }
    evidence["transport_projection"] = projection
    _truncate_log_content(evidence, max_utf8_bytes, projection)
    _truncate_trace_spans(evidence, max_utf8_bytes, projection)
    projected = _encode(evidence)
    if len(projected) > max_utf8_bytes:
        raise LimitExceededError(
            "The Run evidence structure exceeds the current Judge upload limit",
            code="log_size_exceeded",
        )
    return evidence, projected


__all__ = ["project_run_evidence"]
