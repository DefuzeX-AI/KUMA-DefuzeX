"""Pure, bounded conversion from OTel LogRecords to safe log Evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any

from ..repository.privacy import scan_sensitive_text
from .trace_mapping import _safe_attributes, _safe_text

if TYPE_CHECKING:
    from .trace import TraceEvidenceLimits

OTEL_LOG_SCHEMA = "defuzex.otel_logs.v1"
OTEL_LOG_MEDIA_TYPE = "application/vnd.defuzex.otel-logs+json"

_LOG_REASONS = frozenset(
    {
        "otel_log_attribute_filtered",
        "otel_log_byte_limit",
        "otel_log_capture_failed",
        "otel_log_export_failed",
        "otel_log_invalid",
        "otel_log_limit",
        "otel_log_sensitive_filtered",
        "otel_log_value_truncated",
    }
)


class LogRecordMappingError(ValueError):
    """A stable, value-free reason why an OTel LogRecord was unusable."""

    def __init__(self, reason: str = "otel_log_invalid") -> None:
        """Retain an allowlisted reason code without rejected LogRecord content."""
        super().__init__(reason)
        self.reason = reason if reason in _LOG_REASONS else "otel_log_invalid"


@dataclass(frozen=True, slots=True)
class BuiltOtelLogSegment:
    """Return one safe OTel log artifact plus exact capture accounting.

    Attributes:
        segment: Structured hash-only log segment, or ``None`` when no record
            fits safely in the remaining byte budget.
        retained_count: Number of normalized records present in ``segment``.
        dropped_count: Number of observed records omitted by mapping or limits.
        reasons: Stable allowlisted degradation reasons; never raw log text or
            exporter exceptions.
        encoded_size: UTF-8 bytes consumed by the retained segment content.
    """

    segment: Mapping[str, Any] | None
    retained_count: int
    dropped_count: int
    reasons: tuple[str, ...]
    encoded_size: int


def _optional_nano(value: Any) -> tuple[int | None, bool]:
    """Normalize an optional non-negative OTel nanosecond timestamp."""
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, True
    return value, False


def _optional_identifier(value: Any, width: int) -> tuple[str | None, bool]:
    """Normalize an optional trace or span identifier to fixed-width hex."""
    if value in (None, 0):
        return None, False
    maximum = 1 << (width * 4)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < maximum:
        return None, True
    return f"{value:0{width}x}", False


def _severity(value: Any) -> tuple[int | None, str]:
    """Map the standard numeric severity range to a stable public level."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None, "unset"
    if not 0 <= number <= 24:
        return None, "unset"
    if number == 0:
        return 0, "unset"
    names = ("trace", "debug", "info", "warn", "error", "fatal")
    return number, names[min((number - 1) // 4, len(names) - 1)]


def _safe_hash(value: Any, limit: int) -> tuple[str | None, bool, bool, bool]:
    """Hash bounded non-sensitive text without retaining the original log value."""
    if value is None:
        return None, False, False, False
    if isinstance(value, str):
        text = value[: limit + 1]
    elif isinstance(value, bytes):
        text = value[: limit + 1].decode("utf-8", "replace")
    elif isinstance(value, bool | int) or (
        isinstance(value, float) and math.isfinite(value)
    ):
        text = str(value)
    else:
        return None, False, False, True
    bounded_text = (
        text[:limit].encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    )
    if scan_sensitive_text(bounded_text, location="otel_log"):
        return None, False, True, False
    truncated = len(text) > limit
    bounded = bounded_text.encode("utf-8")
    return hashlib.sha256(bounded).hexdigest(), truncated, False, False


def _attribute_count(value: Any, limit: int) -> tuple[int, bool]:
    """Count inspected attributes up to the configured privacy budget."""
    if not isinstance(value, Mapping):
        return 0, value is not None
    try:
        inspected = list(islice(value, limit + 1))
    except Exception:
        return 0, True
    return min(len(inspected), limit), len(inspected) > limit


def _record_core(record: Any) -> tuple[dict[str, Any], set[str]]:
    """Normalize timing, correlation IDs, and severity for one LogRecord."""
    timestamp, invalid_timestamp = _optional_nano(record.timestamp)
    observed, invalid_observed = _optional_nano(record.observed_timestamp)
    trace_id, invalid_trace = _optional_identifier(record.trace_id, 32)
    span_id, invalid_span = _optional_identifier(record.span_id, 16)
    reasons = set()
    if invalid_timestamp or invalid_observed or invalid_trace or invalid_span:
        reasons.add("otel_log_invalid")
    severity_number, severity_name = _severity(getattr(record, "severity_number", None))
    return (
        {
            "timestamp_unix_nano": timestamp,
            "observed_timestamp_unix_nano": observed,
            "trace_id": trace_id,
            "span_id": span_id,
            "severity_number": severity_number,
            "severity": severity_name,
        },
        reasons,
    )


def _record_content(
    record: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, set[str]]:
    """Hash body/event text and count rather than retain arbitrary attributes."""
    body_hash, body_truncated, body_sensitive, body_invalid = _safe_hash(
        getattr(record, "body", None), limits.max_text_length
    )
    event_hash, event_truncated, event_sensitive, event_invalid = _safe_hash(
        getattr(record, "event_name", None), limits.max_text_length
    )
    attribute_count, attributes_truncated = _attribute_count(
        getattr(record, "attributes", None), limits.max_attributes
    )
    reasons = set()
    if body_sensitive or event_sensitive:
        reasons.add("otel_log_sensitive_filtered")
    if body_invalid or event_invalid:
        reasons.add("otel_log_invalid")
    if body_truncated or event_truncated or attributes_truncated:
        reasons.add("otel_log_value_truncated")
    if attribute_count or attributes_truncated:
        reasons.add("otel_log_attribute_filtered")
    return (
        {
            "body_sha256": body_hash,
            "event_name_sha256": event_hash,
            "attribute_count": attribute_count,
        },
        attribute_count + int(attributes_truncated),
        reasons,
    )


def _resource_scope(
    readable: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, set[str]]:
    """Map allowlisted resource metadata and bounded instrumentation scope."""
    reasons = set()
    try:
        attributes = getattr(getattr(readable, "resource", None), "attributes", None)
        resource, dropped, truncated, _ = _safe_attributes(
            attributes, limits, resource=True
        )
    except Exception:
        resource, dropped, truncated = {}, 1, True
    if dropped:
        reasons.add("otel_log_attribute_filtered")
    if truncated:
        reasons.add("otel_log_value_truncated")
    try:
        scope = getattr(readable, "instrumentation_scope", None)
        name, name_truncated = _safe_text(
            getattr(scope, "name", ""), limits.max_text_length
        )
        version, version_truncated = _safe_text(
            getattr(scope, "version", "") or "", limits.max_text_length
        )
    except Exception:
        name, version = "", ""
        name_truncated = version_truncated = True
        reasons.add("otel_log_invalid")
    if name_truncated or version_truncated:
        reasons.add("otel_log_value_truncated")
    return (
        {"resource": resource, "scope": {"name": name, "version": version}},
        dropped,
        reasons,
    )


def map_log_record(
    readable: Any, limits: TraceEvidenceLimits
) -> tuple[dict[str, Any], int, bool, set[str]]:
    """Map one SDK ReadableLogRecord without retaining raw body or attributes."""

    try:
        record = getattr(readable, "log_record", readable)
    except Exception as exc:
        raise LogRecordMappingError() from exc
    try:
        core, reasons = _record_core(record)
    except Exception as exc:
        raise LogRecordMappingError() from exc
    content, dropped, content_reasons = _record_content(record, limits)
    metadata, resource_dropped, metadata_reasons = _resource_scope(readable, limits)
    reasons.update(content_reasons)
    reasons.update(metadata_reasons)
    return (
        {**core, **content, **metadata},
        dropped + resource_dropped,
        bool(reasons),
        reasons,
    )


def build_log_segment(
    *,
    run_id: str,
    input_id: str,
    submission_id: str,
    records: list[Mapping[str, Any]],
    observed_count: int,
    dropped_count: int,
    reasons: set[str],
    max_bytes: int,
) -> BuiltOtelLogSegment:
    """Fit associated, hash-only OTel records into the remaining Run log budget.

    ``TraceEvidenceCapture.prepare_step`` supplies already normalized records.
    This pure projection removes a deterministic suffix until the complete JSON
    envelope fits, recording every omission; it never retains raw bodies,
    attributes, prompts, or secrets.

    Args:
        run_id: Owning Run identifier written into the closed envelope.
        input_id: Owning input/step identifier.
        submission_id: Deterministic Submission correlation identifier.
        records: Normalized hash-only OTel records in stable order.
        observed_count: Total associated records seen before filtering.
        dropped_count: Records already omitted during mapping/capture.
        reasons: Stable allowlisted degradation reasons accumulated so far.
        max_bytes: Remaining UTF-8 byte budget for the complete content.

    Returns:
        Segment and exact retained/dropped/encoded accounting. ``segment`` is
        ``None`` when no record fits safely.

    Preconditions:
        Records came from :func:`map_log_record`, identifiers match one active
        Submission, and ``max_bytes`` is the remaining Run budget.

    Postconditions:
        Serialized content is at most ``max_bytes``. Omitted suffix records
        increase ``dropped_count`` and add ``otel_log_byte_limit``.

    Security/Privacy:
        Only hashes, counts, and allowlisted metadata are serialized; raw body,
        attributes, prompts, and exception text are never reintroduced.
    """

    retained = list(records)
    bounded_reasons = set(reasons) & _LOG_REASONS
    if not retained:
        return BuiltOtelLogSegment(
            segment=None,
            retained_count=0,
            dropped_count=max(dropped_count, observed_count),
            reasons=tuple(sorted(bounded_reasons or {"otel_log_invalid"})),
            encoded_size=0,
        )
    while retained:
        content = _log_content(
            run_id,
            input_id,
            submission_id,
            retained,
            observed_count,
            dropped_count,
            bounded_reasons,
        )
        encoded = content.encode("utf-8")
        if len(encoded) <= max_bytes:
            return BuiltOtelLogSegment(
                segment={
                    "path": "otel://in-process/logs",
                    "segment_no": 0,
                    "start_offset": 0,
                    "end_offset": len(encoded),
                    "encoding": "utf-8",
                    "binary": False,
                    "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                    "complete": not bounded_reasons,
                    "media_type": OTEL_LOG_MEDIA_TYPE,
                    "content": content,
                },
                retained_count=len(retained),
                dropped_count=dropped_count,
                reasons=tuple(sorted(bounded_reasons)),
                encoded_size=len(encoded),
            )
        retained.pop()
        dropped_count += 1
        bounded_reasons.add("otel_log_byte_limit")
    return BuiltOtelLogSegment(
        segment=None,
        retained_count=0,
        dropped_count=max(dropped_count, observed_count),
        reasons=tuple(sorted(bounded_reasons | {"otel_log_byte_limit"})),
        encoded_size=0,
    )


def _log_content(
    run_id: str,
    input_id: str,
    submission_id: str,
    records: list[Mapping[str, Any]],
    observed_count: int,
    dropped_count: int,
    reasons: set[str],
) -> str:
    """Serialize one associated OTel Logs envelope using canonical field order."""
    payload = {
        "schema_version": OTEL_LOG_SCHEMA,
        "run_id": run_id,
        "input_id": input_id,
        "step_id": input_id,
        "submission_id": submission_id,
        "records": records,
        "observed_count": observed_count,
        "retained_count": len(records),
        "dropped_count": dropped_count,
        "truncated": bool(reasons),
        "reasons": sorted(reasons),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def log_record_sort_key(
    item: tuple[int, Mapping[str, Any], int],
) -> tuple[int, int, str, str, int]:
    """Order records by timestamps, correlation IDs, then capture sequence."""
    sequence, record, _ = item
    return (
        record["timestamp_unix_nano"] or 0,
        record["observed_timestamp_unix_nano"] or 0,
        record["trace_id"] or "",
        record["span_id"] or "",
        sequence,
    )


__all__ = [
    "OTEL_LOG_MEDIA_TYPE",
    "OTEL_LOG_SCHEMA",
    "BuiltOtelLogSegment",
    "LogRecordMappingError",
    "build_log_segment",
    "log_record_sort_key",
    "map_log_record",
]
