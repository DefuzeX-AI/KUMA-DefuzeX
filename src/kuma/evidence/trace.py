"""Bounded, transactional in-process Trace Evidence capture."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts import CaptureComponent
from ..errors import ConfigurationError
from .trace_mapping import extract_agent_output, json_size, map_span

_CAPTURE_REASONS = frozenset(
    {
        "trace_attribute_filtered",
        "trace_attribute_invalid",
        "trace_attribute_limit",
        "trace_budget_exhausted",
        "trace_byte_limit",
        "trace_capture_failed",
        "trace_drop_count_saturated",
        "trace_event_limit",
        "trace_export_failed",
        "trace_flush_failed",
        "trace_serialization_failed",
        "trace_span_limit",
        "trace_value_truncated",
    }
)
_MAX_DROPPED_COUNT = 999_999_999


def _evidence_payload(
    run_id: str,
    case_id: str,
    input_id: str,
    *,
    spans: list[Mapping[str, Any]] | None = None,
    dropped_count: int = 0,
    truncated: bool = False,
    reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_version": "defuzex.trace_evidence.v1",
        "run_id": run_id,
        "case_id": case_id,
        "input_id": input_id,
        "spans": [] if spans is None else spans,
        "dropped_count": dropped_count,
        "truncated": truncated,
        "reasons": list(reasons),
    }


_MIN_TRACE_EVIDENCE_BYTES = json_size(_evidence_payload("r", "c", "i"))


@dataclass(frozen=True, slots=True)
class TraceEvidenceLimits:
    """Bound capture; total bytes include compact envelopes committed per Run."""

    max_spans: int = 200
    max_attributes: int = 32
    max_events_per_span: int = 20
    max_text_length: int = 256
    max_total_bytes: int = 512_000

    def __post_init__(self) -> None:
        for name in (
            "max_spans",
            "max_attributes",
            "max_events_per_span",
            "max_text_length",
            "max_total_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"{name} must be a positive integer")
        if self.max_total_bytes < _MIN_TRACE_EVIDENCE_BYTES:
            raise ConfigurationError(
                "max_total_bytes is smaller than the minimum Trace Evidence envelope "
                f"({_MIN_TRACE_EVIDENCE_BYTES} bytes)"
            )


@dataclass(slots=True)
class _Association:
    run_id: str
    case_id: str
    input_id: str
    active: bool = True


@dataclass(slots=True)
class _Bucket:
    spans: list[tuple[int, Mapping[str, Any], int]] = field(default_factory=list)
    next_sequence: int = 0
    bytes_used: int = 0
    dropped_count: int = 0
    truncated: bool = False
    reasons: set[str] = field(default_factory=set)
    agent_output: tuple[tuple[int, int, int], Any] | None = None


@dataclass(slots=True)
class PreparedTraceEvidence:
    evidence: Mapping[str, Any] | None
    component: CaptureComponent
    missing: tuple[str, ...]
    dropped_count: int
    _capture: TraceEvidenceCapture
    _association: _Association | None
    _sequence: int
    _encoded_size: int = 0
    _finished: bool = False

    def commit(self) -> None:
        if self._finished:
            return
        if self._association is not None:
            self._capture._commit(self._association, self._sequence, self._encoded_size)
        self._finished = True

    def abort(self) -> None:
        if self._finished:
            return
        if self._association is not None:
            self._capture._abort(self._association)
        self._finished = True


class TraceEvidenceCapture:
    """Receive mapped ended spans and expose one transaction per Run step."""

    def __init__(self, limits: TraceEvidenceLimits | None = None) -> None:
        self.limits = limits or TraceEvidenceLimits()
        self._lock = threading.RLock()
        self._buckets: dict[int, _Bucket] = {}
        self._run_bytes: dict[tuple[str, str], int] = {}
        self._active: _Association | None = None
        self._registrations: dict[tuple[int, int], _Association] = {}
        self._force_flush: Callable[[], bool] | None = None

    def _set_force_flush(self, callback: Callable[[], bool]) -> None:
        self._force_flush = callback

    def begin_step(self, run_id: str, case_id: str, input_id: str) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (run_id, case_id, input_id)
        ):
            raise ConfigurationError(
                "Trace Evidence Run, Case, and Input IDs must be non-empty text"
            )
        envelope_size = json_size(_evidence_payload(run_id, case_id, input_id))
        if envelope_size > self.limits.max_total_bytes:
            raise ConfigurationError(
                "Trace Evidence identifiers exceed max_total_bytes before capture"
            )
        association = _Association(run_id, case_id, input_id)
        with self._lock:
            if self._active is not None and self._active.active:
                self._discard_locked(self._active)
                raise RuntimeError(
                    "Trace Evidence is already associated with a Run step"
                )
            self._buckets[id(association)] = _Bucket()
            self._active = association

    def register_span(self, span: Any) -> None:
        key = _span_key(span)
        with self._lock:
            association = self._active
            if key is None or association is None or not association.active:
                return
            bucket = self._buckets.get(id(association))
            if bucket is None or key in self._registrations:
                return
            registered = sum(
                candidate is association for candidate in self._registrations.values()
            )
            if len(bucket.spans) + registered >= self.limits.max_spans:
                self._note_drop(bucket, 1, "trace_span_limit", truncated=True)
                return
            self._registrations[key] = association

    def export_registered_span(self, span: Any) -> None:
        key = _span_key(span)
        if key is None:
            return
        with self._lock:
            association = self._registrations.pop(key, None)
        if association is not None:
            self.export_span(span, association=association)

    def export_span(
        self, span: Any, *, association: _Association | None = None
    ) -> None:
        if association is None:
            with self._lock:
                association = self._active
        if association is None or not association.active:
            return
        try:
            mapped, dropped, truncated, reasons = map_span(span, self.limits)
            size = json_size(mapped)
            agent_output = extract_agent_output(span, self.limits.max_total_bytes)
        except Exception:
            self.record_failure("trace_serialization_failed", association=association)
            return
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or not association.active:
                return
            if agent_output is not None:
                rank, output = agent_output
                if bucket.agent_output is None or rank >= bucket.agent_output[0]:
                    bucket.agent_output = (rank, output)
            self._increment_dropped(bucket, dropped)
            bucket.truncated = bucket.truncated or truncated
            bucket.reasons.update(
                reason for reason in reasons if reason in _CAPTURE_REASONS
            )
            if len(bucket.spans) >= self.limits.max_spans:
                self._note_drop(bucket, 1, "trace_span_limit", truncated=True)
                return
            run_bytes = self._run_bytes.get(
                (association.run_id, association.case_id), 0
            )
            if run_bytes + bucket.bytes_used + size > self.limits.max_total_bytes:
                self._note_drop(bucket, 1, "trace_byte_limit", truncated=True)
                return
            sequence = bucket.next_sequence
            bucket.next_sequence += 1
            bucket.spans.append((sequence, mapped, size))
            bucket.bytes_used += size

    def output_for_step(self, run_id: str, case_id: str, input_id: str) -> Any | None:
        """Return the latest standard OTel Agent/Workflow output for this step."""

        association = self._matching_association(run_id, case_id, input_id)
        self._flush(association)
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or bucket.agent_output is None:
                return None
            return bucket.agent_output[1]

    def record_failure(
        self, reason: str, *, association: _Association | None = None
    ) -> None:
        with self._lock:
            target = association or self._active
            if target is None or not target.active:
                return
            bucket = self._buckets.get(id(target))
            if bucket is not None:
                bounded_reason = (
                    reason if reason in _CAPTURE_REASONS else "trace_capture_failed"
                )
                self._note_drop(bucket, 1, bounded_reason)

    def prepare_step(
        self, run_id: str, case_id: str, input_id: str
    ) -> PreparedTraceEvidence:
        association = self._matching_association(run_id, case_id, input_id)
        self._flush(association)
        sequence, spans, reasons, dropped, truncated = self._snapshot(association)
        evidence, reasons, dropped, encoded_size = self._fit_evidence(
            association,
            spans,
            reasons,
            dropped,
            truncated,
        )
        status = "failed" if evidence is None else "complete"
        if evidence is not None and reasons:
            status = "partial" if evidence["spans"] else "failed"
        return PreparedTraceEvidence(
            evidence=evidence,
            component=CaptureComponent(status=status, reasons=reasons),
            missing=tuple(f"trace_evidence:{reason}" for reason in reasons),
            dropped_count=dropped,
            _capture=self,
            _association=association,
            _sequence=sequence,
            _encoded_size=encoded_size,
        )

    def _fit_evidence(
        self,
        association: _Association,
        spans: list[Mapping[str, Any]],
        reasons: tuple[str, ...],
        dropped: int,
        truncated: bool,
    ) -> tuple[Mapping[str, Any] | None, tuple[str, ...], int, int]:
        ordered = sorted(
            spans,
            key=lambda item: (
                item["start_time_unix_nano"],
                item["trace_id"],
                item["span_id"],
            ),
        )
        bounded_reasons = set(reasons) & _CAPTURE_REASONS
        run_key = (association.run_id, association.case_id)
        with self._lock:
            available = self.limits.max_total_bytes - self._run_bytes.get(run_key, 0)
        evidence = self._build_evidence(
            association, ordered, dropped, truncated, bounded_reasons
        )
        while ordered and json_size(evidence) > available:
            ordered.pop()
            if dropped >= _MAX_DROPPED_COUNT:
                bounded_reasons.add("trace_drop_count_saturated")
            else:
                dropped += 1
            truncated = True
            bounded_reasons.add("trace_byte_limit")
            evidence = self._build_evidence(
                association, ordered, dropped, truncated, bounded_reasons
            )
        encoded_size = json_size(evidence)
        if encoded_size <= available:
            return evidence, tuple(sorted(bounded_reasons)), dropped, encoded_size
        bounded_reasons.add("trace_budget_exhausted")
        if dropped >= _MAX_DROPPED_COUNT:
            bounded_reasons.add("trace_drop_count_saturated")
        dropped = min(dropped + 1, _MAX_DROPPED_COUNT)
        return None, tuple(sorted(bounded_reasons)), dropped, 0

    @staticmethod
    def _build_evidence(
        association: _Association,
        spans: list[Mapping[str, Any]],
        dropped: int,
        truncated: bool,
        reasons: set[str],
    ) -> dict[str, Any]:
        return _evidence_payload(
            association.run_id,
            association.case_id,
            association.input_id,
            spans=spans,
            dropped_count=dropped,
            truncated=truncated,
            reasons=tuple(sorted(reasons)),
        )

    def _matching_association(
        self, run_id: str, case_id: str, input_id: str
    ) -> _Association:
        with self._lock:
            association = self._active
            expected = (run_id, case_id, input_id)
            actual = None
            if association is not None:
                actual = (association.run_id, association.case_id, association.input_id)
            if association is None or not association.active or actual != expected:
                raise RuntimeError(
                    "Trace Evidence association does not match this step"
                )
            return association

    def _flush(self, association: _Association) -> None:
        if self._force_flush is not None:
            try:
                if not self._force_flush():
                    self.record_failure("trace_flush_failed", association=association)
            except Exception:
                self.record_failure("trace_flush_failed", association=association)

    def _snapshot(
        self, association: _Association
    ) -> tuple[int, list[Mapping[str, Any]], tuple[str, ...], int, bool]:
        with self._lock:
            association.active = False
            if self._active is association:
                self._active = None
            self._drop_registrations_locked(association)
            bucket = self._buckets[id(association)]
            sequence = bucket.next_sequence - 1
            spans = [item[1] for item in bucket.spans if item[0] <= sequence]
            reasons = tuple(sorted(bucket.reasons))
            dropped = bucket.dropped_count
            truncated = bucket.truncated
        return sequence, spans, reasons, dropped, truncated

    def cancel_step(self) -> None:
        with self._lock:
            association = self._active
            if association is not None:
                self._discard_locked(association)

    def finish_run(self, run_id: str, case_id: str) -> None:
        with self._lock:
            association = self._active
            if association is not None and (
                association.run_id,
                association.case_id,
            ) == (run_id, case_id):
                self._discard_locked(association)
            self._run_bytes.pop((run_id, case_id), None)

    def _commit(
        self, association: _Association, sequence: int, encoded_size: int
    ) -> None:
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None:
                return
            run_key = (association.run_id, association.case_id)
            self._run_bytes[run_key] = self._run_bytes.get(run_key, 0) + encoded_size
            retained = [item for item in bucket.spans if item[0] > sequence]
            if retained:
                bucket.spans = retained
                bucket.bytes_used = sum(item[2] for item in retained)
            else:
                self._buckets.pop(id(association), None)

    def _abort(self, association: _Association) -> None:
        with self._lock:
            if id(association) not in self._buckets:
                return
            if self._active is not None and self._active is not association:
                raise RuntimeError("Another Trace Evidence step is already active")
            association.active = True
            self._active = association

    def _discard_locked(self, association: _Association) -> None:
        association.active = False
        if self._active is association:
            self._active = None
        self._drop_registrations_locked(association)
        self._buckets.pop(id(association), None)

    def _drop_registrations_locked(self, association: _Association) -> None:
        stale = [
            key
            for key, candidate in self._registrations.items()
            if candidate is association
        ]
        for key in stale:
            self._registrations.pop(key, None)

    @staticmethod
    def _increment_dropped(bucket: _Bucket, count: int) -> None:
        if count <= 0:
            return
        total = bucket.dropped_count + count
        bucket.dropped_count = min(total, _MAX_DROPPED_COUNT)
        if total > _MAX_DROPPED_COUNT:
            bucket.reasons.add("trace_drop_count_saturated")

    def _note_drop(
        self, bucket: _Bucket, count: int, reason: str, *, truncated: bool = False
    ) -> None:
        self._increment_dropped(bucket, count)
        bucket.reasons.add(
            reason if reason in _CAPTURE_REASONS else "trace_capture_failed"
        )
        bucket.truncated = bucket.truncated or truncated


def _span_key(span: Any) -> tuple[int, int] | None:
    context = getattr(span, "context", None)
    if context is None:
        get_context = getattr(span, "get_span_context", None)
        context = get_context() if callable(get_context) else None
    trace_id = getattr(context, "trace_id", None)
    span_id = getattr(context, "span_id", None)
    if (
        isinstance(trace_id, bool)
        or not isinstance(trace_id, int)
        or isinstance(span_id, bool)
        or not isinstance(span_id, int)
    ):
        return None
    return trace_id, span_id


__all__ = ["PreparedTraceEvidence", "TraceEvidenceCapture", "TraceEvidenceLimits"]
